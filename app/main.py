"""
Homelab Dashboard: the web app.

How the code fits together
--------------------------
serve.py      starts everything: HTTPS on the dashboard's port, passing connections to this app.
main.py       (this file) the FastAPI app itself, plus the live readings: CPU, memory and network,
              Docker containers, filesystems and pools, drive health (SMART), the file browser's
              reading side, service checks and the "needs attention" list on the Overview.
security.py   sign-in, sessions, the password-again check for big changes, and the page headers.
passkeys.py   signing in (and confirming) with a passkey instead of the password.
tls.py        the HTTPS certificate: your own, Tailscale's, or one made here.
upkeep.py     settings, scheduled restarts, alerts, Docker clean-up and the activity log.
metrics.py    GPU and power readings, and the history behind the Overview charts.
drivecare.py  the pools' drives: failing drives, replacing them, scrubs and SMART self-tests.
zfsmgr.py     pool and dataset management, snapshots, scheduled scrubs and TRIMs.
fileops.py    changing files from the file browser: uploads, moves, copies, the Trash.
static/       the pages (index.html is the whole dashboard; login.html, setup.html) and fonts.

The other modules reach this one (and each other) through small helpers such as M() that look the
module up in sys.modules when called, rather than importing it, because they're imported from the
bottom of this file and a plain import would be circular.

Background threads (started in _start at the bottom): system_sampler, container_poller and
smart_poller here, plus each module's own loop. They read the host every few seconds while a page
is open and slow right down when nobody is looking.

Everything the dashboard remembers is in the data folder (DASH_DATA, /data in the container):
settings.json, state.json, events.json (activity log), overrides.json (names you've given things),
drivecare.json, zfs.json, trash.json, moves.json, uploads.json, passkeys.json, revoked.json,
session.key, history.db (charts), app-icon.png and tls/ (certificates).
"""
import fnmatch
import json
import mimetypes
import os
import re
import socket
import ssl
import stat
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import psutil
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

try:
    import docker
except ImportError:  # pragma: no cover
    docker = None

# --------------------------------------------------------------------------- config
CONFIG_PATH = os.environ.get("DASH_CONFIG", "/config/config.json")
BROWSE_ROOT = Path(os.environ.get("DASH_BROWSE_ROOT", "/browse"))
HOST_NS = os.environ.get("DASH_HOST_NS", "1") == "1"  # run df/zpool/lsblk inside the host mount namespace
AUTH_USER = os.environ.get("DASH_USER") or None  # sign-in is on when both of these are set
AUTH_PASS = os.environ.get("DASH_PASS") or None
SMART_INTERVAL = int(os.environ.get("DASH_SMART_INTERVAL", "600"))  # seconds between drive health checks
STATIC = Path(__file__).parent / "static"

# names the file browser never shows (config.json → files.deny replaces this list): keys and secrets
DEFAULT_DENY = [".ssh", ".gnupg", ".aws", ".kube", ".password-store", ".env", "*.env",
                "*.pem", "*.key", "id_rsa*", "id_ed25519*", "id_ecdsa*", ".docker", ".git-credentials"]

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")  # colour codes in container logs

app = FastAPI(title="Homelab Dashboard", docs_url=None, redoc_url=None, openapi_url=None)  # no API description published


_config = {"sig": None, "data": {}, "deny": None}


def load_config():
    """config.json (your services, drive labels, file-browser deny list). Read again only when the file
    changes, since it's consulted for every name in a folder listing. Shared: don't change what this
    returns. A broken file gives {"_error": "..."}, which the page shows as a banner."""
    try:
        st = os.stat(CONFIG_PATH)
        sig = (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        sig = None
    if sig != _config["sig"]:
        try:
            with open(CONFIG_PATH) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {"_error": "config.json should hold one { ... } object"}
        except FileNotFoundError:
            data = {}
        except json.JSONDecodeError as e:
            data = {"_error": f"config.json is not valid JSON: {e}"}
        _config.update(sig=sig, data=data, deny=None)
    return _config["data"]


_stuck = set()  # commands that wouldn't die when killed (stuck inside the kernel on a failing drive)


def host_cmd(args, timeout=15):
    """Run a command, inside the host's mount namespace when available. Gives up after `timeout`
    seconds: the command is killed, and if it can't even be killed (a zpool stuck on a dead drive
    sits in the kernel where kill can't reach it until the drive answers), it's left behind rather
    than holding this thread forever."""
    return run_bounded((["nsenter", "-t", "1", "-m", "--"] + args) if HOST_NS else args, timeout)


def run_bounded(cmd, timeout=15):
    """subprocess.run(capture_output, text, timeout) that can't hang on a child that won't die."""
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        try:
            p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            _stuck.add(p)  # kept so it's reaped if it ever finishes
            for o in (p.stdout, p.stderr):
                try:
                    o.close()
                except Exception:
                    pass
        raise subprocess.TimeoutExpired(cmd, timeout)
    finally:
        for q in list(_stuck):
            if q.poll() is not None:
                _stuck.discard(q)
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


def parse_docker_time(s):
    """Docker's timestamp text (nanoseconds, 'Z' for UTC) as a Unix time, or None for 'never' (0001-01-01)."""
    if not s or s.startswith("0001-"):
        return None
    try:
        s = s.replace("Z", "+00:00")
        m = re.match(r"^(.*\.\d{6})\d*(\+.*)$", s)
        if m:
            s = m.group(1) + m.group(2)
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


# --------------------------------------------------------------------------- sign-in
import security  # noqa: E402

app.middleware("http")(security.guard)
app.add_middleware(security.Compress)
app.add_middleware(security.BodyLimit)  # outermost: counts bytes even for chunked uploads
app.include_router(security.router)


# --------------------------------------------------------------------------- system sampler
SPARK_WINDOW = 300  # seconds of history behind the small Overview graphs
_sys_lock = threading.Lock()
_sys = {"history": deque(maxlen=1200), "current": {}}
_viewing = {"t": 0.0}


def refresh_ms():
    """How often the page refreshes the live figures (Settings → Display), in milliseconds."""
    try:
        return int(upkeep.setting("display", "refresh_ms", 2000))
    except Exception:
        return 2000


def live_interval(idle=2.0):
    """Seconds between readings: the Settings choice while a dashboard page is open, otherwise
    no faster than `idle`, so a closed dashboard costs next to nothing."""
    fast = refresh_ms() / 1000
    return fast if time.time() - _viewing["t"] < 15 else max(fast, idle)


_phys = {"t": 0.0, "nics": {}}


def _is_physical(nic):
    """A real network card (not a bridge or tunnel). Cards don't come and go often: checked once a minute."""
    if time.time() - _phys["t"] > 60:
        _phys.update(t=time.time(), nics={})
    hit = _phys["nics"].get(nic)
    if hit is None:
        hit = _phys["nics"][nic] = os.path.exists(f"/sys/class/net/{nic}/device")
    return hit


def _skip_nic(nic):
    """Virtual network cards (loopback, Docker bridges, VPN plumbing) that aren't listed or counted."""
    return nic == "lo" or nic.startswith(("veth", "br-", "docker", "virbr", "cali", "flannel", "cni"))


def _host_info():
    """The machine's name, Ubuntu version, kernel and CPU model, read once at start-up."""
    info = {"hostname": socket.gethostname(), "os": None, "kernel": os.uname().release, "cpu_model": None}
    try:
        with open("/proc/sys/kernel/hostname") as f:
            info["hostname"] = f.read().strip() or info["hostname"]
    except OSError:
        pass
    try:
        r = host_cmd(["cat", "/etc/os-release"], timeout=5)
        m = re.search(r'^PRETTY_NAME="?([^"\n]+)', r.stdout, re.M)
        info["os"] = m.group(1) if m else None
    except Exception:
        pass
    try:
        with open("/proc/cpuinfo") as f:
            m = re.search(r"^model name\s*:\s*(.+)$", f.read(), re.M)
            model = re.sub(r"\((?:R|TM)\)|\bCPU\b", "", m.group(1), flags=re.I) if m else None
            info["cpu_model"] = re.sub(r"\s+", " ", model).strip() if model else None
    except OSError:
        pass
    info["cores_physical"] = psutil.cpu_count(logical=False)
    info["cores_logical"] = psutil.cpu_count(logical=True)
    return info


HOST_INFO = _host_info()


_temp = {"t": 0.0, "v": None}


def _cpu_temp():
    """CPU package temperature. Reading the sensors touches dozens of files, so at most every 2 s."""
    if time.time() - _temp["t"] < 2:
        return _temp["v"]
    v = None
    try:
        temps = psutil.sensors_temperatures()
        for key in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz"):
            for t in temps.get(key, []):
                if key != "coretemp" or t.label.startswith("Package") or not t.label:
                    v = round(t.current, 1)
                    break
            if v is not None:
                break
    except Exception:
        pass
    _temp.update(t=time.time(), v=v)
    return v


def slow_after():
    """Seconds of missing readings that count as the server slowing down (Settings → Alerts)."""
    try:
        return max(5, int(upkeep.setting("alerts", "slow_secs") or 10))
    except Exception:
        return 10


def system_sampler():
    """Background thread: CPU, memory and network readings, at the page's refresh rate while a page is open
    (every 2 s otherwise). Keeps the last few minutes for the small Overview graphs, and notices when the
    machine was too busy to take a reading on time (a stall: see metrics.on_stall)."""
    psutil.cpu_percent(percpu=True)
    prev_t, prev_io = time.time(), psutil.net_io_counters(pernic=True)
    prev_m, prev_ct = time.monotonic(), psutil.cpu_times()
    boot = psutil.boot_time()
    nic_up, nic_t = {}, 0.0
    while True:
        want = live_interval(2.0)
        time.sleep(want)
        try:
            per_core = psutil.cpu_percent(percpu=True)
            now = time.time()
            # a reading that comes far later than asked for means the machine was too busy to run us
            mono, ct = time.monotonic(), psutil.cpu_times()
            if (mono - prev_m) - want >= slow_after():
                threading.Thread(target=metrics.on_stall, args=(prev_t, now, prev_ct, ct), daemon=True).start()
            prev_m, prev_ct = mono, ct
            io = psutil.net_io_counters(pernic=True)
            if now - nic_t > 10:  # whether each card is up: every 10 s is plenty
                try:
                    nic_up, nic_t = {k: v.isup for k, v in psutil.net_if_stats().items()}, now
                except Exception:
                    nic_up = {}
            dt = max(now - prev_t, 0.001)
            nics, tot_rx, tot_tx = [], 0.0, 0.0
            for name, c in io.items():
                if _skip_nic(name):
                    continue
                p = prev_io.get(name)
                rx = max(0.0, (c.bytes_recv - p.bytes_recv) / dt) if p else 0.0
                tx = max(0.0, (c.bytes_sent - p.bytes_sent) / dt) if p else 0.0
                phys = _is_physical(name)
                if phys:
                    tot_rx += rx
                    tot_tx += tx
                nics.append({"name": name, "physical": phys, "up": nic_up.get(name, True), "rx_rate": rx, "tx_rate": tx,
                             "rx_total": c.bytes_recv, "tx_total": c.bytes_sent})
            prev_t, prev_io = now, io
            vm, sw = psutil.virtual_memory(), psutil.swap_memory()
            cpu = sum(per_core) / max(len(per_core), 1)
            current = {
                "time": now,
                "cpu": {"percent": round(cpu, 1), "per_core": per_core, "temp": _cpu_temp(),
                        "load": os.getloadavg()},
                "memory": {"total": vm.total, "used": vm.total - vm.available, "available": vm.available,
                           "cached": getattr(vm, "cached", 0) + getattr(vm, "buffers", 0),
                           "percent": vm.percent, "swap_total": sw.total, "swap_used": sw.used},
                "network": {"rx_rate": tot_rx, "tx_rate": tot_tx, "interfaces": nics},
                "uptime": now - boot,
            }
            try:  # GPU load too, so its small graph survives a page reload like the others
                gpus = [round(g["util"]) if g.get("util") is not None else None for g in metrics.fresh_gpus(now)]
            except Exception:
                gpus = []
            with _sys_lock:
                _sys["current"] = current
                _sys["history"].append({"t": round(now, 2), "cpu": round(cpu, 1), "mem": vm.percent,
                                        "rx": round(tot_rx), "tx": round(tot_tx), "gpu": gpus})
        except Exception as e:  # keep sampling no matter what
            print("system sampler error:", e, flush=True)


@app.get("/api/system")
def api_system(history: bool = False, since: float | None = None):
    """Live figures. `since`: also every reading taken after that moment (within the last few minutes),
    so a page that was in the background, or just reloaded, fills its small graphs without gaps."""
    _viewing["t"] = time.time()
    with _sys_lock:
        out = {"host": HOST_INFO, **_sys["current"], **metrics.live(), "refresh_ms": refresh_ms(),
               "gaps": metrics.recent_gaps(SPARK_WINDOW)}
        if history or since is not None:
            cut = time.time() - SPARK_WINDOW
            out["history"] = [h for h in _sys["history"] if h["t"] >= cut and (since is None or h["t"] > since)]
    return out


# --------------------------------------------------------------------------- docker
_dc = None
_dc_lock = threading.Lock()
_containers = {"items": [], "error": None, "updated": 0}


def dclient():
    """The Docker client, made on first use (and again after an error, in case Docker restarted)."""
    global _dc
    with _dc_lock:
        if _dc is None:
            if docker is None:
                raise RuntimeError("docker python package missing")
            _dc = docker.from_env(timeout=10)
        return _dc


_cpu_prev = {}            # container id -> (container cpu total, system cpu total) from the last poll
_one_shot = {"ok": True}  # Docker 20.10+ can answer stats instantly instead of sampling for a second


def _container_stats(c):
    """One container's CPU %, memory, network and disk totals from Docker's stats. CPU % is worked out from the
    change since the previous reading."""
    try:
        if _one_shot["ok"]:
            try:
                s = c.stats(stream=False, one_shot=True)
            except (TypeError, docker.errors.InvalidVersion, docker.errors.APIError):
                _one_shot["ok"] = False
                s = c.stats(stream=False)
        else:
            s = c.stats(stream=False)
    except Exception:
        return {}
    cpu, pre = s.get("cpu_stats") or {}, s.get("precpu_stats") or {}
    tot, sysu = (cpu.get("cpu_usage") or {}).get("total_usage"), cpu.get("system_cpu_usage")
    if pre.get("system_cpu_usage"):  # the slow path gives us the previous sample
        prev = ((pre.get("cpu_usage") or {}).get("total_usage", 0), pre["system_cpu_usage"])
    else:
        prev = _cpu_prev.get(c.id)
    if tot is not None and sysu:
        _cpu_prev[c.id] = (tot, sysu)
    cd = (tot - prev[0]) if prev and tot is not None else 0
    sd = (sysu - prev[1]) if prev and sysu else 0
    cpu_pct = (cd / sd) * 100 if sd > 0 and cd >= 0 else 0.0  # % of whole host
    mem = s.get("memory_stats") or {}
    st = mem.get("stats") or {}
    inactive = st.get("inactive_file", st.get("total_inactive_file", 0))
    used = max(mem.get("usage", 0) - inactive, 0)
    rx = tx = 0
    for n in (s.get("networks") or {}).values():
        rx += n.get("rx_bytes", 0)
        tx += n.get("tx_bytes", 0)
    br = bw = 0
    for e in ((s.get("blkio_stats") or {}).get("io_service_bytes_recursive") or []):
        op = (e.get("op") or "").lower()
        if op == "read":
            br += e.get("value", 0)
        elif op == "write":
            bw += e.get("value", 0)
    return {"cpu": round(cpu_pct, 2), "mem": used, "mem_limit": mem.get("limit", 0),
            "net_rx": rx, "net_tx": tx, "blk_read": br, "blk_write": bw,
            "pids": (s.get("pids_stats") or {}).get("current")}


def _ports(attrs):
    """A container's published ports as [{host, container, ip}], without duplicates (IPv4 and IPv6 bindings)."""
    out, seen = [], set()
    for cport, binds in ((attrs.get("NetworkSettings") or {}).get("Ports") or {}).items():
        for b in binds or []:
            key = (b.get("HostPort"), cport)
            if key in seen:
                continue
            seen.add(key)
            out.append({"host": b.get("HostPort"), "container": cport, "ip": b.get("HostIp")})
    return out


_stats_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ctr-stats")
_inspected = {}  # container id -> (what the list said about it, when, its full details)


def _details(cl, c):
    """A container's full details (Docker's inspect), asked for again only when the container list shows
    a change (started, stopped, health, exit code) or once a minute, rather than for every container
    every few seconds."""
    a = c.attrs
    status = a.get("Status") or ""
    health = re.search(r"\((healthy|unhealthy|health: starting)\)", status)
    code = re.match(r"Exited \((-?\d+)\)", status)
    key = (a.get("State") if isinstance(a.get("State"), str) else None, health.group(1) if health else None,
           code.group(1) if code else None, a.get("Created"))
    hit = _inspected.get(c.id)
    if hit and hit[0] == key and time.time() - hit[1] < 60:
        return hit[2]
    full = cl.api.inspect_container(c.id)
    _inspected[c.id] = (key, time.time(), full)
    return full


def container_poller():
    """Every 3 s while a dashboard page is open; otherwise every 10 s without CPU and memory figures
    (those every 30 s), which is all the crash and restart alerts need."""
    prev, last_stats, stats = {}, 0.0, {}
    while True:
        viewing = time.time() - _viewing["t"] < 15
        try:
            cl = dclient()
            cs = cl.containers.list(all=True, sparse=True)
            now = time.time()
            fresh = viewing or now - last_stats >= 30
            if fresh:
                running = [c for c in cs if c.attrs.get("State") == "running"]
                stats = dict(zip([c.id for c in running], _stats_pool.map(_container_stats, running)))
                last_stats = now
            items = []
            for c in cs:
                try:
                    a = _details(cl, c)
                except docker.errors.NotFound:
                    continue  # removed between the list and the look (a compose re-create): skip, not a failed poll
                state = a.get("State") or {}
                labels = (a.get("Config") or {}).get("Labels") or {}
                st = dict(stats.get(c.id) or {}) if state.get("Running") else {}
                p = prev.get(c.id)
                if st and fresh and "net_rx" in st:  # network speed since the last time the figures were read
                    if p and now > p["t"]:
                        dt = now - p["t"]
                        st["net_rx_rate"] = max(0, (st["net_rx"] - p["net_rx"]) / dt)
                        st["net_tx_rate"] = max(0, (st["net_tx"] - p["net_tx"]) / dt)
                    prev[c.id] = {"t": now, "net_rx": st["net_rx"], "net_tx": st["net_tx"],
                                  "rx_rate": st.get("net_rx_rate"), "tx_rate": st.get("net_tx_rate")}
                elif st and p:
                    st["net_rx_rate"], st["net_tx_rate"] = p.get("rx_rate"), p.get("tx_rate")
                started = parse_docker_time(state.get("StartedAt"))
                nets = (a.get("NetworkSettings") or {}).get("Networks") or {}
                items.append({
                    "id": c.id[:12],
                    "name": (a.get("Name") or "").lstrip("/") or c.id[:12],
                    "image": (a.get("Config") or {}).get("Image"),
                    "state": state.get("Status"),
                    "health": (state.get("Health") or {}).get("Status"),
                    "exit_code": state.get("ExitCode"),
                    "started": started,
                    "uptime": (now - started) if started and state.get("Running") else None,
                    "restarts": a.get("RestartCount", 0),
                    "project": labels.get("com.docker.compose.project"),
                    "service": labels.get("com.docker.compose.service"),
                    "network_mode": (a.get("HostConfig") or {}).get("NetworkMode"),
                    "ports": _ports(a),
                    "ip": next((n.get("IPAddress") for n in nets.values() if n.get("IPAddress")), None),
                    "exposed": list(((a.get("NetworkSettings") or {}).get("Ports") or {}).keys()),
                    "mounts": [m["Source"] for m in a.get("Mounts") or [] if m.get("Source")],  # host folders it uses
                    "stats": st,
                })
            alive = {c.id for c in cs}
            for d in (prev, _inspected, _cpu_prev, stats):  # forget containers that are gone
                for k in [k for k in d if k not in alive]:
                    d.pop(k, None)
            _containers.update(items=items, error=None, updated=now)
        except Exception as e:
            _containers.update(error=f"{type(e).__name__}: {e}", updated=time.time())
            global _dc
            with _dc_lock:
                _dc = None
            time.sleep(5)
        time.sleep(3 if viewing else 10)


@app.get("/api/containers")
def api_containers():
    """Every container with its state and figures (from container_poller), and the names you've given them."""
    names = load_overrides().get("containers", {})
    return {**_containers, "items": [dict(c, display=names.get(c["name"]), is_self=bool(SELF_ID) and SELF_ID.startswith(c["id"]))
                                     for c in _containers["items"]]}


@app.get("/api/containers/{cid}/logs")
def api_container_logs(cid: str, tail: int = Query(300, ge=10, le=5000)):
    """The last lines of a container's log, as plain text with the colour codes taken out."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", cid):
        raise HTTPException(400, "bad id")
    try:
        c = dclient().containers.get(cid)
        raw = c.logs(tail=tail, timestamps=True)
    except Exception as e:
        raise HTTPException(404, str(e))
    return PlainTextResponse(ANSI_RE.sub("", raw.decode("utf-8", "replace")))


def _self_container_id():
    """This dashboard's own container ID (Docker bind-mounts /etc/hostname etc. from its folder)."""
    try:
        with open("/proc/self/mountinfo") as f:
            m = re.search(r"/(?:docker/)?containers/([0-9a-f]{64})/", f.read())
            return m.group(1) if m else None
    except OSError:
        return None


SELF_ID = _self_container_id()
_action_lock = threading.Lock()
_busy = set()


@app.post("/api/containers/{cid}/action")
def api_container_action(cid: str, body: dict = Body(...)):
    """Start, stop or restart a container. The dashboard can restart itself (it answers first, then goes) but not
    stop itself."""
    action = body.get("action")
    if action not in ("restart", "stop", "start"):
        raise HTTPException(400, "action must be restart, stop or start")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", cid):
        raise HTTPException(400, "bad id")
    try:
        c = dclient().containers.get(cid)
    except Exception as e:
        raise HTTPException(404, f"No such container: {e}")
    name = load_overrides().get("containers", {}).get(c.name) or c.name
    is_self = bool(SELF_ID) and c.id == SELF_ID
    if is_self and action == "stop":
        raise HTTPException(409, "The dashboard can't stop itself, or there'd be no way to start it again from here.")
    if is_self and action == "restart":
        upkeep.log("container", f"Restarted {name} (the dashboard) from the dashboard")
        threading.Timer(1.0, lambda: c.restart(timeout=10)).start()  # answer first, then go
        return {"ok": True, "self": True}
    with _action_lock:
        if c.id in _busy:
            raise HTTPException(409, f"{name} is already being {action}ed")
        _busy.add(c.id)
    try:
        if action == "start":
            c.start()
        else:
            getattr(c, action)(timeout=30)  # 30 s to shut down cleanly (game servers save their world)
    except Exception as e:
        msg = getattr(e, "explanation", None) or str(e)
        upkeep.log("error", f"Couldn't {action} {name}: {msg}")
        raise HTTPException(500, f"Couldn't {action} {name}: {msg}")
    finally:
        with _action_lock:
            _busy.discard(c.id)
    upkeep.mark_stopped_on_purpose(c.name, action == "stop")
    verb = {"restart": "Restarted", "stop": "Stopped", "start": "Started"}[action]
    upkeep.log("container", f"{verb} {name} from the dashboard")
    return {"ok": True}


# --------------------------------------------------------------------------- storage
FS_EXCLUDE = ["tmpfs", "devtmpfs", "squashfs", "overlay", "efivarfs", "nsfs", "proc", "sysfs",
              "cgroup2", "fuse.snapfuse", "fuse.lxcfs", "ramfs", "autofs"]
_storage_cache = {"data": None, "t": 0}


def _df():
    """Mounted filesystems from the host's df, skipping virtual ones (tmpfs, overlay, snaps and so on)."""
    args = ["df", "-B1", "-P", "-T"] + sum((["-x", t] for t in FS_EXCLUDE), [])
    r = host_cmd(args)
    out = []
    for line in r.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 7:
            continue
        try:
            size, used, avail = int(parts[2]), int(parts[3]), int(parts[4])
        except ValueError:
            continue
        mnt = " ".join(parts[6:])
        if size == 0 or mnt.startswith(("/var/lib/docker", "/snap/", "/run/", "/sys/", "/proc/")):
            continue
        out.append({"source": parts[0], "type": parts[1], "mount": mnt, "size": size, "used": used,
                    "free": avail, "percent": round(used / (used + avail) * 100, 1) if used + avail else 0})
    if not out and r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "df failed")
    return out


def _zpools():
    """Each ZFS pool's size, usable space, health, latest scrub and errors, for the Storage page."""
    r = host_cmd(["zpool", "list", "-H", "-p", "-o", "name,size,alloc,free,frag,cap,health"])
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()
        if "no pools available" in msg or not msg:
            return []
        raise RuntimeError(msg)
    rows = [f for f in (ln.split("\t") for ln in r.stdout.strip().splitlines()) if len(f) >= 7]
    if not rows:
        return []
    # zpool list reports raw capacity (parity included for RAIDZ); zfs list gives usable space. One call for every pool.
    usable = {}
    try:
        zl = host_cmd(["zfs", "list", "-H", "-p", "-o", "name,used,available"] + [f[0] for f in rows])
        for ln in zl.stdout.splitlines():
            n, u, a = (ln.split("\t") + ["", "", ""])[:3]
            usable[n] = (int(u), int(a))
    except Exception:
        pass
    # drive care reads the status text anyway: use its copy if it's recent
    snap = drivecare._snap
    status = {p["name"]: p.get("status_text") or "" for p in snap["data"][0]} if snap["data"] and time.time() - snap["t"] < 15 else {}

    def num(x):
        try:
            return int(x)
        except ValueError:
            return None
    pools = []
    for f in rows:
        name = f[0]
        st = status.get(name)
        if st is None:
            st = host_cmd(["zpool", "status", "-P", name]).stdout
        scan = re.search(r"^\s*scan:\s*(.+(?:\n\s{2,}(?!config:).+)*)", st, re.M)
        errs = re.search(r"^errors:\s*(.+)$", st, re.M)
        used, avail = usable.get(name, (None, None))
        size = (used + avail) if used is not None else None
        pools.append({"name": name, "usable": size, "used": used, "avail": avail,
                      "percent": round(used / size * 100, 1) if size else None,
                      "size": num(f[1]), "alloc": num(f[2]), "free": num(f[3]),
                      "frag": num(f[4]), "cap": num(f[5]), "health": f[6],
                      "scan": re.sub(r"\s+", " ", scan.group(1)).strip() if scan else None,
                      "errors": errs.group(1).strip() if errs else None, "status": st.strip()})
    return pools


def _fs_hidden_by_default(f):
    """ZFS datasets are hidden on Storage unless they hold at least 1 GB (their pool shows the space anyway)."""
    if f["type"] != "zfs":
        return False
    return "/" not in f["source"] or f["used"] < 1e9  # pool root, or a dataset using under 1 GB


def _with_visibility(data):
    """Marks each filesystem shown or hidden: your choice where you've made one, otherwise the default."""
    choice = load_overrides().get("filesystems", {})
    fs = []
    for f in data["filesystems"]:
        c = choice.get(f["mount"])
        fs.append(dict(f, hidden=(c == "hidden") if c else _fs_hidden_by_default(f), hidden_choice=c))
    return {**data, "filesystems": fs}


@app.put("/api/filesystems/visibility")
def api_fs_visibility(body: dict = Body(...)):
    """Show or hide one filesystem on the Storage page."""
    mount, visible = str(body.get("mount") or ""), body.get("visible")
    if not mount.startswith("/") or len(mount) > 512 or not isinstance(visible, bool):
        raise HTTPException(400, "mount and visible are required")
    with _ov_lock:
        ov = _overrides_for_change()
        section = ov.setdefault("filesystems", {})
        if mount not in section and len(section) >= MAX_OVERRIDES:
            raise HTTPException(400, "Too many saved choices")
        section[mount] = "shown" if visible else "hidden"
        _save_overrides(ov)
    return {"ok": True}


@app.get("/api/storage")
def api_storage():
    """Filesystems and pools, for the Storage page."""
    return _with_visibility(_storage_raw())


def _storage_raw():
    """Filesystems and pools, read at most every 15 s however many pages ask. Falls back to the container's own
    view of the disks if df can't run on the host."""
    if _storage_cache["data"] and time.time() - _storage_cache["t"] < 15:
        return _storage_cache["data"]
    data = {"filesystems": [], "pools": [], "errors": []}
    try:
        data["filesystems"] = _df()
    except Exception as e:
        data["errors"].append(f"Filesystems: {e}")
        for p in psutil.disk_partitions(all=False):  # fallback: container view
            try:
                u = psutil.disk_usage(p.mountpoint)
                data["filesystems"].append({"source": p.device, "type": p.fstype, "mount": p.mountpoint,
                                            "size": u.total, "used": u.used, "free": u.free, "percent": u.percent})
            except OSError:
                pass
    try:
        data["pools"] = _zpools()
    except FileNotFoundError:
        pass
    except Exception as e:
        if "not found" not in str(e) and "No such file" not in str(e):
            data["errors"].append(f"ZFS: {e}")
    _storage_cache.update(data=data, t=time.time())
    return data


# --------------------------------------------------------------------------- SMART
_drives = {"items": [], "error": None, "updated": 0, "refreshing": False}
_smart_event = threading.Event()


def _lsblk_usage():
    """serial -> {'mounts': [...], 'pools': [...], 'path': '/dev/x'} from the host's lsblk."""
    try:
        r = host_cmd(["lsblk", "-J", "-o", "NAME,PATH,SERIAL,FSTYPE,LABEL,MOUNTPOINTS,TYPE"], timeout=10)
        tree = json.loads(r.stdout).get("blockdevices", [])
    except Exception:
        return {}
    out = {}

    def walk(node, acc):
        for m in node.get("mountpoints") or []:
            if m and m not in acc["mounts"]:
                acc["mounts"].append(m)
        if node.get("fstype") == "zfs_member" and node.get("label") and node["label"] not in acc["pools"]:
            acc["pools"].append(node["label"])
        if node.get("fstype") in ("LVM2_member", "linux_raid_member") and node.get("fstype") not in acc["other"]:
            acc["other"].append(node["fstype"])
        for ch in node.get("children") or []:
            walk(ch, acc)

    for d in tree:
        if d.get("type") != "disk":
            continue
        acc = {"mounts": [], "pools": [], "other": [], "path": d.get("path")}
        walk(d, acc)
        if d.get("serial"):
            out[d["serial"].strip()] = acc
    return out


DRIVE_LIMITS = {"nvme_hot": 70, "hdd_hot": 55, "wear_warn": 70, "wear_bad": 90}


def _self_test(j):
    """What the drive says about its own self-tests: one running now, and the latest result."""
    out = {}
    ata = (j.get("ata_smart_data") or {}).get("self_test") or {}
    status = ata.get("status") or {}
    if status:
        out["running"] = (int(status.get("value") or 0) >> 4) == 0xF
        if out["running"] and status.get("remaining_percent") is not None:
            out["percent"] = 100 - int(status["remaining_percent"])
        out["minutes"] = {"short": (ata.get("polling_minutes") or {}).get("short"), "long": (ata.get("polling_minutes") or {}).get("extended")}
    table = (((j.get("ata_smart_self_test_log") or {}).get("standard") or {}).get("table") or [])
    if table:
        e = table[0]
        st = e.get("status") or {}
        text = st.get("string") or ""
        out["last"] = {"type": (e.get("type") or {}).get("string"), "result": text, "hours": e.get("lifetime_hours"),
                       "passed": bool(st.get("passed")), "failed": st.get("passed") is False and not re.search(r"abort|interrupt|progress", text, re.I)}
    nv = j.get("nvme_self_test_log")
    if nv:
        op = int(((nv.get("current_self_test_operation") or {}).get("value")) or 0)
        out["running"] = op != 0
        if out["running"] and nv.get("current_self_test_completion_percent") is not None:
            out["percent"] = int(nv["current_self_test_completion_percent"])
        rows = nv.get("table") or []
        if rows:
            e = rows[0]
            res = e.get("self_test_result") or {}
            v = int(res.get("value") or 0)
            out["last"] = {"type": (e.get("self_test_code") or {}).get("string"), "result": res.get("string"),
                           "hours": e.get("power_on_hours"), "passed": v == 0, "failed": v in (5, 6, 7)}  # others: stopped early, not failed
    return out or None


def _parse_smart(j, limits=None):
    """One drive's smartctl JSON, reduced to what the page shows, with a verdict (good, warn or bad) and the
    reasons for it. `limits` are the temperature and wear thresholds from Settings → Drive care."""
    lim = {**DRIVE_LIMITS, **(limits or {})}
    dev = j.get("device") or {}
    d = {
        "device": dev.get("name"), "protocol": dev.get("protocol"),
        "model": j.get("model_name") or j.get("model_family") or j.get("scsi_model_name"),
        "serial": j.get("serial_number"),
        "firmware": j.get("firmware_version"),
        "capacity": (j.get("user_capacity") or {}).get("bytes") or j.get("nvme_total_capacity"),
        "passed": (j.get("smart_status") or {}).get("passed"),
        "temp": (j.get("temperature") or {}).get("current"),
        "power_on_hours": (j.get("power_on_time") or {}).get("hours"),
        "power_cycles": j.get("power_cycle_count"),
        "rotation": j.get("rotation_rate"),
    }
    nv = j.get("nvme_smart_health_information_log")
    if nv:
        d.update(kind="nvme", wear=nv.get("percentage_used"), spare=nv.get("available_spare"),
                 spare_threshold=nv.get("available_spare_threshold"), media_errors=nv.get("media_errors"),
                 unsafe_shutdowns=nv.get("unsafe_shutdowns"), critical_warning=nv.get("critical_warning"),
                 written=(nv.get("data_units_written") or 0) * 512000,
                 read=(nv.get("data_units_read") or 0) * 512000)
    else:
        d["kind"] = "hdd" if (d["rotation"] or 0) > 0 else "ssd"
    attrs = {a.get("id"): a for a in ((j.get("ata_smart_attributes") or {}).get("table") or [])}
    for key, aid in (("reallocated", 5), ("pending", 197), ("uncorrectable", 198), ("crc_errors", 199)):
        if aid in attrs:
            d[key] = (attrs[aid].get("raw") or {}).get("value")
    if d["kind"] == "ssd":
        for aid in (231, 233, 177, 202):  # vendor-specific life left / wear levelling
            if aid in attrs and attrs[aid].get("value") is not None:
                d["life_left"] = attrs[aid]["value"]
                break
    if 241 in attrs:  # total LBAs written (most SATA SSDs)
        d["written"] = ((attrs[241].get("raw") or {}).get("value") or 0) * (j.get("logical_block_size") or 512)

    issues, level = [], "good"

    def flag(msg, lvl):
        nonlocal level
        issues.append(msg)
        if lvl == "bad" or (lvl == "warn" and level == "good"):
            level = lvl
    if d["passed"] is False:
        flag("SMART overall-health test FAILED", "bad")
    if d.get("critical_warning"):
        flag(f"NVMe critical warning flag {d['critical_warning']}", "bad")
    if (d.get("media_errors") or 0) > 0:
        flag(f"{d['media_errors']} media/data-integrity errors", "bad")
    if d.get("spare") is not None and d.get("spare_threshold") is not None and d["spare"] <= d["spare_threshold"]:
        flag("Available spare below threshold", "bad")
    if (d.get("uncorrectable") or 0) > 0:
        flag(f"{d['uncorrectable']} offline-uncorrectable sectors", "bad")
    if (d.get("pending") or 0) > 0:
        flag(f"{d['pending']} pending (unstable) sectors", "warn")
    if (d.get("reallocated") or 0) > 0:
        flag(f"{d['reallocated']} reallocated sectors", "warn")
    if (d.get("crc_errors") or 0) > 0:
        flag(f"{d['crc_errors']} interface CRC errors (check cable)", "warn")
    if d.get("wear") is not None:
        if d["wear"] >= lim["wear_bad"]:
            flag(f"{d['wear']}% of rated endurance used", "bad")
        elif d["wear"] >= lim["wear_warn"]:
            flag(f"{d['wear']}% of rated endurance used", "warn")
    hot = lim["nvme_hot"] if d["kind"] == "nvme" else lim["hdd_hot"]
    if d["temp"] is not None and d["temp"] >= hot:
        flag(f"Running hot ({d['temp']}°C)", "warn")
    d["self_test"] = _self_test(j)
    if ((d["self_test"] or {}).get("last") or {}).get("failed"):
        flag(f"Its last self-test failed ({d['self_test']['last'].get('result')})", "bad")
    d["issues"], d["status"], d["hot_at"] = issues, level, hot
    d["wear_warn"], d["wear_bad"] = lim["wear_warn"], lim["wear_bad"]
    return d


def _smart_scan():
    """Every drive's health, from smartctl in this container. Drives that are asleep (standby) aren't woken up:
    their last reading is kept."""
    r = subprocess.run(["smartctl", "--scan-open", "-j"], capture_output=True, text=True, timeout=30)
    devices = (json.loads(r.stdout or "{}").get("devices")) or []
    usage = _lsblk_usage()
    labels = (load_config().get("drive_labels") or {})
    previous = {d.get("device"): d for d in _drives["items"]}
    try:
        limits = {k: upkeep.load_settings()["drivecare"][k] for k in DRIVE_LIMITS}
    except Exception:
        limits = None
    items = []
    for dev in devices:
        name, dtype = dev.get("name"), dev.get("type")
        if not name:
            continue
        args = ["smartctl", "-j", "-a", "-n", "standby"] + (["-d", dtype] if dtype else []) + [name]
        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=60)
            j = json.loads(p.stdout or "{}")
        except Exception as e:
            items.append({"device": name, "status": "unknown", "issues": [str(e)]})
            continue
        msgs = " ".join(m.get("string", "") for m in ((j.get("smartctl") or {}).get("messages") or []))
        if "STANDBY" in msgs.upper() and not j.get("serial_number"):
            old = dict(previous.get(name) or {"device": name, "status": "unknown", "issues": []})
            old["standby"] = True
            items.append(old)
            continue
        d = _parse_smart(j, limits)
        d["standby"], d["dtype"] = False, dtype
        u = usage.get((d.get("serial") or "").strip(), {})
        d["mounts"], d["pools"], d["block_path"] = u.get("mounts", []), u.get("pools", []), u.get("path")
        d["label"] = labels.get(d.get("serial") or "") or labels.get(name) or labels.get(u.get("path") or "")
        items.append(d)
    items.sort(key=lambda x: (x.get("label") is None, x.get("label") or "", x.get("device") or ""))
    return items


def smart_poller():
    """Background thread: re-reads drive health every DASH_SMART_INTERVAL seconds (10 minutes), or straight away
    when something sets _smart_event (Re-check drives, a drive change, new limits)."""
    while True:
        _drives["refreshing"] = True
        try:
            _drives.update(items=_smart_scan(), error=None, updated=time.time())
        except FileNotFoundError:
            _drives.update(error="smartctl not installed in this container", updated=time.time())
        except Exception as e:
            _drives.update(error=f"{type(e).__name__}: {e}", updated=time.time())
        _drives["refreshing"] = False
        _smart_event.wait(SMART_INTERVAL)
        _smart_event.clear()


@app.get("/api/drives")
def api_drives():
    """Every drive's health, with the names you've given them."""
    names = load_overrides().get("drives", {})
    items = [dict(d, display=names.get(d.get("serial") or d.get("device") or "")) for d in _drives["items"]]
    return {**_drives, "items": items}


@app.post("/api/drives/refresh")
def api_drives_refresh():
    """Re-check drive health now (Storage → Re-check drives)."""
    _smart_event.set()
    return {"ok": True}


# --------------------------------------------------------------------------- file browser
def _deny_re():
    """The file browser's deny list (config.json → files.deny, or DEFAULT_DENY) as one compiled pattern,
    rebuilt only when config.json changes. Lower-case: ".SSH" or "ID_RSA" on a case-insensitive disk
    are the same files."""
    load_config()  # refreshes _config (and clears the compiled pattern) if the file changed
    if _config["deny"] is None:
        pats = (_config["data"].get("files") or {}).get("deny", DEFAULT_DENY)
        if not isinstance(pats, list):
            pats = DEFAULT_DENY
        _config["deny"] = re.compile("|".join(fnmatch.translate(str(p).lower()) for p in pats) or r"(?!)")
    return _config["deny"]


def _denied(name):
    """True for a name on the deny list (keys, .env files and the like): hidden and never served."""
    return bool(_deny_re().match(name.lower()))


def private_from_mounts(mounts, labels, alone_in_project, browse_root="/browse", data="/data",
                        config="/config/config.json", certs="/certs"):
    """Host folders/files that belong to the dashboard itself, as paths under the file browser.
    Its compose file holds the password and its data folder the session key, so the browser hides them."""
    binds = [m for m in mounts if m.get("Type") == "bind" and m.get("Source") and m.get("Destination")]
    browse = [(m["Source"].rstrip("/") or "/", m["Destination"]) for m in binds
              if m["Destination"].startswith(browse_root.rstrip("/") + "/")]
    config_dir = os.path.dirname(config.rstrip("/"))  # config.json is often mounted as a folder (./config:/config)
    own = {m["Source"] for m in binds if m["Destination"] in (data, config, certs)
           or (config_dir not in ("", "/") and m["Destination"].rstrip("/") == config_dir)}
    files = [f.strip() for f in (labels.get("com.docker.compose.project.config_files") or "").split(",") if f.strip()]
    own.update(files)
    workdir = labels.get("com.docker.compose.project.working_dir")
    if workdir:
        own.add(f"{workdir.rstrip('/')}/.env")  # compose reads variables (like DASH_PASS) from here
        if alone_in_project:  # the dashboard has a folder of its own: hide all of it
            own.add(workdir)
    out = set()
    for host in own:
        host = host.rstrip("/") or "/"
        for src, dest in browse:
            if src == "/" or host == src or host.startswith(src + "/"):
                out.add(dest + host[len(src):] if src != "/" else dest + host)
    return sorted(out)


_private = {"t": 0.0, "paths": frozenset(), "ok": False}


def private_paths():
    """The dashboard's own files as they appear in the file browser. If Docker can't be asked right
    now, the last answer is kept (and if there's never been one, files aren't served at all) rather
    than briefly treating nothing as private."""
    if time.time() - _private["t"] < 300:
        if not _private["ok"]:
            raise HTTPException(503, "The file browser can't tell which folders are the dashboard's own right now "
                                     "(Docker isn't answering), so it's closed for a moment. Try again shortly.")
        return _private["paths"]
    found, ok = set(), True
    try:
        if SELF_ID and docker is not None:
            c = dclient().containers.get(SELF_ID)
            labels = (c.attrs.get("Config") or {}).get("Labels") or {}
            proj = labels.get("com.docker.compose.project")
            alone = bool(proj) and len(dclient().containers.list(all=True, filters={"label": f"com.docker.compose.project={proj}"})) <= 1
            for q in private_from_mounts(c.attrs.get("Mounts") or [], labels, alone, str(BROWSE_ROOT), str(DATA_DIR),
                                         str(CONFIG_PATH), str(tls.USER_CERTS)):
                found.add(Path(q))
                try:
                    found.add(Path(q).resolve())
                except (OSError, RuntimeError):
                    pass
    except Exception:
        ok = False
    for q in (load_config().get("files") or {}).get("hide") or []:  # extra folders to hide, as /browse/... paths
        if isinstance(q, str) and q.startswith("/"):
            found.add(Path(q))
    if ok:
        _private.update(t=time.time(), paths=frozenset(found), ok=True)
    elif _private["ok"]:
        _private["t"] = time.time() - 270  # keep the last good answer, ask again in 30 s
    else:
        _private.update(t=time.time() - 285, ok=False)  # ask again in 15 s
        return private_paths()
    return _private["paths"]


def _is_private(p: Path):
    """Whether a path is, or is inside, one of the dashboard's own folders or files."""
    priv = private_paths()
    return bool(priv) and (p in priv or any(q in priv for q in p.parents))


SNAPDIR = ".zfs"  # ZFS snapshot folders: old copies of everything, the dashboard's own files included
# the temporary names fileops uses while it works: .dash-upload-<id>.part, .dash-edit-<id>.tmp,
# .dash-moving-<id> (and a copy's "name.dash-moving-<id>")
WORKING_RE = re.compile(r"\.(.+\.)?dash-(upload|moving|edit)-[0-9a-f]{8,24}(\.part|\.tmp)?")


def _hidden_part(part):
    # also the dashboard's own work in progress: half-uploaded files, copies being made, edits being saved
    """A name the file browser never shows or touches: ZFS snapshot folders, the Trash, the dashboard's own
    half-finished work, and names on the deny list."""
    return part in (SNAPDIR, ".dashboard-trash") or bool(WORKING_RE.fullmatch(part)) or _denied(part)


def safe_path(rel: str) -> Path:
    """A path from the page (relative to the browsable folders), checked and resolved: inside BROWSE_ROOT,
    nothing hidden or private on the way there, and it exists. Raises 400, 403 or 404 otherwise."""
    root = BROWSE_ROOT.resolve()
    rel = (rel or "").strip().lstrip("/")
    if len(rel) > 4096 or "\x00" in rel:
        raise HTTPException(400, "Bad path")
    try:
        p = (root / rel).resolve()
    except (OSError, RuntimeError, ValueError):  # symlink loops, odd names
        raise HTTPException(400, "Bad path")
    if p != root and root not in p.parents:
        raise HTTPException(403, "Outside the browsable folders")
    for part in p.relative_to(root).parts + Path(rel).parts:
        if part == SNAPDIR:
            raise HTTPException(403, "ZFS snapshot folders (.zfs) aren't browsable here: they hold old copies of "
                                     "everything, including the dashboard's own password and keys")
        if _hidden_part(part):
            raise HTTPException(403, "This path is hidden by the dashboard's deny list")
    unresolved = root / rel
    if _is_private(p) or _is_private(unresolved):
        raise HTTPException(403, "This is the dashboard's own folder (it holds its password and keys), so it's hidden here")
    if not p.exists():
        raise HTTPException(404, "Not found")
    return p


@app.get("/api/files")
def api_files(path: str = ""):
    """One folder's contents for the file browser: folders first, then files, by name. Hidden names,
    the dashboard's own files and links leading outside the browsable folders are left out."""
    root = BROWSE_ROOT.resolve()
    if not root.exists():
        return {"path": "", "entries": [], "error": f"Nothing mounted at {BROWSE_ROOT}"}
    p = safe_path(path)
    if not p.is_dir():
        raise HTTPException(400, "Not a directory")
    entries, truncated = [], False
    try:
        fd = fileops._open_nofollow(p)  # opened without following links, then checked: nothing outside can be listed
    except PermissionError:
        raise HTTPException(403, "Permission denied")
    except OSError:
        raise HTTPException(409, "The folder changed while opening it. Try again.")
    try:
        if (fileops._real(fd) or p) != p:
            raise HTTPException(409, "The folder changed while opening it. Try again.")
        try:
            names = os.listdir(fd)
        except PermissionError:
            raise HTTPException(403, "Permission denied")
        priv = private_paths()  # the folder itself passed safe_path, so each entry only needs checking on its own
        base = "" if p == root else str(p.relative_to(root)) + "/"
        for i, n in enumerate(names):
            if i >= 5000:
                truncated = True
                break
            if _hidden_part(n) or (priv and (p / n) in priv):
                continue
            try:
                st = os.lstat(n, dir_fd=fd)
            except OSError:
                continue
            if not stat.S_ISLNK(st.st_mode):
                d = stat.S_ISDIR(st.st_mode)
                entries.append({"name": n, "path": base + n, "dir": d, "link": False,
                                "size": None if d else st.st_size, "mtime": st.st_mtime})
                continue
            # links: shown only if they lead somewhere inside the browsable folders
            try:
                tgt = (p / n).resolve()
                if tgt == root or root in tgt.parents:
                    if any(_hidden_part(x) for x in tgt.relative_to(root).parts) or _is_private(tgt):
                        continue
                    ts = tgt.stat()
                    d = stat.S_ISDIR(ts.st_mode)
                    entries.append({"name": n, "path": base + n, "dir": d, "link": True,
                                    "size": None if d else ts.st_size, "mtime": ts.st_mtime})
            except (OSError, RuntimeError):
                pass
    finally:
        os.close(fd)
    entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    rel = "" if p == root else str(p.relative_to(root))
    return {"path": rel, "entries": entries, "truncated": truncated, "writable": _writable(p, root)}


def _writable(p: Path, root: Path):
    """Whether things can be changed in this folder from the dashboard: changing files is on, it's
    inside one of the folders (not the list of them), and it isn't mounted read-only."""
    if not fileops.WRITE or p == root:
        return False
    try:
        return not (os.statvfs(p).f_flag & os.ST_RDONLY) and os.access(p, os.W_OK)
    except OSError:
        return False


def regular_file(p: Path) -> Path:
    """Only plain files: opening a named pipe or a device would hang or misbehave."""
    try:
        mode = p.stat().st_mode
    except OSError:
        raise HTTPException(404, "Not found")
    if stat.S_ISDIR(mode):
        raise HTTPException(400, "Is a directory")
    if not stat.S_ISREG(mode):
        raise HTTPException(400, "Not a regular file")
    return p


def open_checked(rel: str):
    """Open a file for reading, then check the file actually opened is the one that was checked:
    if a folder on the way was swapped for a link between checking and opening, the opened file
    could be somewhere else (the dashboard's own folder, say). Returns (fd, path, stat result)."""
    p = regular_file(safe_path(rel))
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except PermissionError:
        raise HTTPException(403, "Permission denied")
    except OSError:
        raise HTTPException(409, "The file changed while opening it. Try again.")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise HTTPException(409, "The file changed while opening it. Try again.")
        root = BROWSE_ROOT.resolve()
        try:
            real = Path(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:
            real = None
        if real is None:  # no /proc: at least make sure it's the same file that was checked
            now = os.stat(p, follow_symlinks=False)
            if (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino):
                raise HTTPException(409, "The file changed while opening it. Try again.")
        elif (real != root and root not in real.parents) or any(_hidden_part(x) for x in real.relative_to(root).parts) \
                or _is_private(real):
            raise HTTPException(403, "Outside the browsable folders")
        return fd, p, st
    except BaseException:
        os.close(fd)
        raise


class _FdFile(FileResponse):
    """Sends an already-open (and checked) file, and closes it afterwards however the sending ends."""

    def __init__(self, fd, st, **kw):
        self._fd = fd
        super().__init__(f"/proc/self/fd/{fd}", stat_result=st, **kw)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.close()

    def close(self):
        """Close the file once (sending finished, failed or never started)."""
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def __del__(self):
        self.close()


@app.get("/api/files/preview")
def api_file_preview(path: str):
    """The start of a file for the preview pane: up to 512 KB of text (or just 'binary'), and whether it can be
    edited here."""
    fd, p, st = open_checked(path)
    limit = 512 * 1024
    with os.fdopen(fd, "rb") as f:
        head = f.read(limit)
    size = st.st_size
    if b"\x00" in head[:8192]:
        return {"kind": "binary", "size": size}
    utf8 = True
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        text, utf8 = head.decode("latin-1"), False
    root = BROWSE_ROOT.resolve()
    link = (root / path.strip().lstrip("/")).is_symlink()
    return {"kind": "text", "size": size, "truncated": size > limit, "text": text, "mtime_ns": str(st.st_mtime_ns),
            "editable": utf8 and size <= limit and not link and _writable(p.parent, root)}


# kinds of file a browser would run as a page or script if opened. They're also sandboxed (no scripts,
# no access to the dashboard), but shown as plain text is safer still. SVG pictures stay pictures (an
# <img> never runs their scripts, and opened on their own they're sandboxed).
ACTIVE_TYPES = {"text/html", "application/xhtml+xml", "text/xml", "application/xml", "text/javascript",
                "application/javascript", "application/x-javascript", "text/xsl", "application/xslt+xml", "text/vtt+html"}


@app.get("/api/files/raw")
def api_file_raw(path: str, download: bool = False):
    """A file itself: shown in the browser (pictures, video, text), or downloaded with download=1."""
    fd, p, st = open_checked(path)
    kind = mimetypes.guess_type(p.name)[0] or "text/plain"
    if download:
        return _FdFile(fd, st, filename=p.name, media_type=kind)
    if kind in ACTIVE_TYPES or (kind != "image/svg+xml" and kind.endswith(("+xml", "/xml", "javascript", "ecmascript"))):
        kind = "text/plain; charset=utf-8"  # a page or script someone uploaded is shown as text, never run
    return _FdFile(fd, st, media_type=kind, headers={"Content-Disposition": "inline", "X-Content-Type-Options": "nosniff",
                                                     "Content-Security-Policy": "sandbox"})


_du_slots = threading.BoundedSemaphore(2)  # sizing a big folder reads every file: two at a time at most


@app.get("/api/files/dirsize")
def api_dirsize(path: str):
    """A folder's total size (du), for Folder size. Two at a time at most, and 2 minutes each."""
    p = safe_path(path)
    if not _du_slots.acquire(timeout=1):
        raise HTTPException(429, "Already measuring two folders. Try again when they're done.")
    try:
        r = run_bounded(["du", "-sb", "--", str(p)], 120)
        return {"bytes": int(r.stdout.split()[0]), "partial": r.returncode != 0}
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Took longer than 2 minutes")
    except (ValueError, IndexError):
        raise HTTPException(500, "Could not size folder")
    finally:
        _du_slots.release()


# --------------------------------------------------------------------------- services
RAKNET_MAGIC = bytes.fromhex("00ffff00fefefefefdfdfdfd12345678")
_svc_cache = {"data": None, "t": 0}
_svc_lock = threading.Lock()


def bedrock_ping(host, port, timeout=2.5):
    """Asks a Minecraft Bedrock server how it's doing (a RakNet 'unconnected ping'): version, players, world."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        pkt = b"\x01" + struct.pack(">q", int(time.time() * 1000)) + RAKNET_MAGIC + struct.pack(">q", 0x5AD)
        s.sendto(pkt, (host, int(port)))
        data, _ = s.recvfrom(4096)
    if not data or data[0] != 0x1C:
        raise RuntimeError("unexpected reply")
    ln = struct.unpack(">H", data[33:35])[0]
    f = data[35:35 + ln].decode("utf-8", "replace").split(";")
    info = {"motd": f[1] if len(f) > 1 else None, "version": f[3] if len(f) > 3 else None}
    try:
        info["players"], info["max_players"] = int(f[4]), int(f[5])
    except (IndexError, ValueError):
        pass
    if len(f) > 7:
        info["world"] = f[7]
    return info


def default_gateway():
    """The host's default router (network_mode: host makes /proc/net/route the host's table)."""
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                p = line.split()
                if len(p) > 3 and p[1] == "00000000" and int(p[3], 16) & 2:
                    return socket.inet_ntoa(struct.pack("<L", int(p[2], 16)))
    except (OSError, ValueError):
        pass
    return None


def find_container(ref):
    """A container from the latest poll by its name or compose service name, or None."""
    for c in _containers["items"]:
        if ref in (c["name"], c.get("service")):
            return c
    return None


def _http_probe(url, timeout):
    """Up if anything below HTTP 500 answers. If the page itself misbehaves but the port is open
    (some router admin pages do), count it as up rather than down."""
    if urllib.parse.urlsplit(url or "").scheme not in ("http", "https"):
        raise ValueError("only http:// and https:// addresses can be checked")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (homelab-dash)"})
    try:
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        return code < 500, f"HTTP {code}"
    except Exception as first:
        u = urllib.parse.urlsplit(url)
        port = u.port or (443 if u.scheme == "https" else 80)
        try:
            with socket.create_connection((u.hostname, port), timeout=3):
                return True, "Reachable (page was slow or unusual)"
        except OSError:
            raise first


def _udp_port(ctr, default=19132):
    """The UDP port a Bedrock container listens on (19132, unless it exposes another)."""
    exposed = ctr.get("exposed") or []
    if f"{default}/udp" in exposed:
        return default
    for p in exposed:
        if p.endswith("/udp"):
            return int(p.split("/")[0])
    return default


def _reason(e):
    """A short reason a check failed: 'no reply', 'connection refused' or the error itself."""
    r = getattr(e, "reason", None) or e
    if isinstance(r, socket.timeout) or "timed out" in str(r):
        return "no reply"
    if isinstance(r, ConnectionRefusedError) or "refused" in str(r):
        return "connection refused"
    return (str(r)[:100] or type(e).__name__)


def check_service(svc):
    """Returns {state: up|degraded|down|unknown, detail, latency, info}."""
    gw = default_gateway()
    sub = lambda v: v.replace("{gateway}", gw or "router-not-found") if isinstance(v, str) else v
    url = sub(svc.get("url"))
    chk = svc.get("check")
    if chk is None:
        chk = {"type": "http", "url": url} if url and not svc.get("container") else {"type": "none"}
    if isinstance(chk, str):
        chk = {"type": "http", "url": chk}
    chk = {k: sub(v) for k, v in chk.items()}
    kind = chk.get("type", "http")
    res = {"state": "unknown", "latency": None, "detail": None}

    ctr = None
    if svc.get("container"):
        ctr = find_container(svc["container"])
        if ctr is None:
            res["detail"] = "Docker not reachable" if _containers.get("error") else f"No container named {svc['container']}"
            res["state"] = "unknown" if _containers.get("error") else "down"
            return res
        if ctr["state"] != "running":
            res.update(state="down", detail=f"Container {ctr['state']}" + (f" ({ctr['exit_code']})" if ctr["state"] == "exited" else ""))
            return res

    if kind == "none":
        if ctr:
            res.update(state="degraded" if ctr.get("health") == "unhealthy" else "up",
                       detail="Container unhealthy" if ctr.get("health") == "unhealthy" else "Container running")
        return res

    t0 = time.perf_counter()
    try:
        if kind == "http":
            ok, detail = _http_probe(chk.get("url") or url, chk.get("timeout", 6))
        elif kind == "tcp":
            with socket.create_connection((chk["host"], int(chk["port"])), timeout=chk.get("timeout", 3)):
                pass
            ok, detail = True, None
        elif kind == "bedrock":
            host, port = chk.get("host"), chk.get("port")
            if ctr and not host:
                # talk to the container directly so Docker's port forwarding can't get in the way
                if ctr.get("network_mode") == "host" or not ctr.get("ip"):
                    host = "127.0.0.1"
                else:
                    host, port = ctr["ip"], port or _udp_port(ctr)
            info = bedrock_ping(host or "127.0.0.1", port or 19132)
            res["info"] = info
            ok = True
            detail = f"{info['players']}/{info['max_players']} players" if "players" in info else None
        else:
            res["detail"] = f"Unknown check type '{kind}' in config.json"
            return res
        res["latency"] = round((time.perf_counter() - t0) * 1000)
    except Exception as e:
        ok, detail = False, _reason(e)

    if ok:
        unhealthy = ctr is not None and ctr.get("health") == "unhealthy"
        res.update(state="degraded" if unhealthy else "up", detail=("Container unhealthy" if unhealthy else detail))
    elif ctr:
        res.update(state="degraded", detail=f"Container running, but {detail}")
    else:
        res.update(state="down", detail=detail)
    return res


@app.get("/api/services")
def api_services(fresh: bool = False):
    """Each service from config.json with its status. All checked at once, at most every 20 s unless ?fresh=true."""
    with _svc_lock:
        if not fresh and _svc_cache["data"] and time.time() - _svc_cache["t"] < 20:
            return _svc_cache["data"]
        cfg = load_config()
        svcs = [s for s in (cfg.get("services") or []) if isinstance(s, dict) and s.get("name")]
        with ThreadPoolExecutor(max_workers=16) as ex:
            results = list(ex.map(check_service, svcs))
        names = load_overrides().get("services", {})
        gw = default_gateway() or "router-not-found"
        items = []
        for s, r in zip(svcs, results):
            item = {k: s.get(k) for k in ("name", "description", "icon", "group", "container")}
            item.update(url=(s.get("url") or "").replace("{gateway}", gw) or None, display=names.get(s["name"]), status=r)
            items.append(item)
        data = {"items": items, "error": cfg.get("_error"), "title": cfg.get("title")}
        _svc_cache.update(data=data, t=time.time())
        return data


# --------------------------------------------------------------------------- display names
DATA_DIR = Path(os.environ.get("DASH_DATA", "/data"))
_ov_lock = threading.Lock()
MAX_OVERRIDES = 500  # per kind: far more than any homelab has, small enough that the file stays tiny


_overrides = {"sig": None, "data": {}}


def load_overrides():
    """overrides.json: the names you've given containers, services and drives, and which filesystems
    are shown on Storage. {"containers": {name: display name}, "services": ..., "drives": {serial: ...},
    "filesystems": {mount: "shown" | "hidden"}}. Read again only when the file changes (the container
    list asks for it every few seconds). Shared: don't change what this returns."""
    p = DATA_DIR / "overrides.json"
    try:
        st = p.stat()
        sig = (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        sig = None
    if sig != _overrides["sig"]:
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            data = {}
        _overrides.update(sig=sig, data=data if isinstance(data, dict) else {})
    return _overrides["data"]


def _overrides_for_change():
    """A private copy of the overrides to change and save (call with _ov_lock held)."""
    return json.loads(json.dumps(load_overrides()))


def _save_overrides(ov):
    """Save overrides.json (call with _ov_lock held)."""
    try:
        upkeep._write("overrides.json", ov)  # written whole to a temporary file first, so it's never half-saved
    except OSError as e:
        raise HTTPException(500, f"Couldn't save to {DATA_DIR}: {e}")


@app.put("/api/names")
def api_rename(body: dict = Body(...)):
    """Give a container, service or drive a name of your own (an empty name goes back to the real one)."""
    kind, key, name = body.get("kind"), body.get("key"), body.get("name") or ""
    if kind not in ("containers", "services", "drives"):
        raise HTTPException(400, "kind must be containers, services or drives")
    if not isinstance(key, str) or not key or len(key) > 200:
        raise HTTPException(400, "Bad key")
    if not isinstance(name, str):
        raise HTTPException(400, "The name has to be text")
    name = re.sub(r"\s+", " ", name).strip()[:60]
    with _ov_lock:
        ov = _overrides_for_change()
        section = ov.setdefault(kind, {})
        if name and key not in section and len(section) >= MAX_OVERRIDES:
            raise HTTPException(400, "Too many saved names")
        if name and name != key:
            section[key] = name
        else:
            section.pop(key, None)
        _save_overrides(ov)
    _svc_cache["t"] = 0  # the services list carries display names: rebuild it
    return {"ok": True, "name": name if name and name != key else None}


# --------------------------------------------------------------------------- needs attention
_attn = {"t": 0.0, "data": None}


def _eta(text):
    """zpool's '1 days 02:03:04' (time to go) as '26 h 3 min'."""
    if not text:
        return None
    d = re.match(r"(?:(\d+) days? )?(\d+):(\d\d):\d\d", text)
    if not d:
        return text
    mins = int(d.group(1) or 0) * 1440 + int(d.group(2)) * 60 + int(d.group(3))
    return f"{mins // 60} h {mins % 60} min" if mins >= 60 else f"{max(mins, 1)} min"


@app.get("/api/attention")
def api_attention():
    """Everything that needs you right now, worst first: for the top of the Overview."""
    if _attn["data"] is not None and time.time() - _attn["t"] < 8:
        return _attn["data"]
    items = []
    try:
        pools = drivecare.snapshot()[0]
    except Exception:
        pools = []
    known = drivecare._load().get("known") or {}
    case_pools = set()
    for c in drivecare.open_cases():
        name = drivecare.case_name(c, known)
        case_pools.add(c["pool"])
        if c["stage"] == "replacing":
            sc = (next((p for p in pools if p["name"] == c["pool"]), None) or {}).get("scan") or {}
            prog = f"{sc['percent']:.0f}% done" if sc.get("percent") is not None else "starting"
            items.append({"level": "info", "title": f"Rebuilding pool {c['pool']}",
                          "detail": f"Replacing {name}: {prog}" + (f", about {_eta(sc.get('eta'))} to go" if sc.get("eta") else ""),
                          "link": f"#storage:replace={c['guid']}", "action": "Follow"})
        else:
            title = {"failed": f"{name} has failed", "offline": f"{name} is offline, ready to swap",
                     "failing": f"{name} is failing"}[c["stage"]]
            detail = c.get("reason") or ""
            if c["stage"] != "failing":
                detail += f". Pool {c['pool']} has no redundancy until it's replaced."
            items.append({"level": "warn" if c["stage"] == "failing" else "bad", "title": title, "detail": detail,
                          "link": f"#storage:replace={c['guid']}", "action": "Replace"})
    s = upkeep.load_settings()
    a = {**s["alerts"], **{k: True for k in ("drives", "pools", "disk_space", "pool_space", "containers", "services")}}
    try:
        conds = upkeep._conditions(a)
    except Exception:
        conds = {}
    links = {"drive": "#storage:drive=", "pool": "#storage", "disk": "#storage", "container": "#containers", "service": "#services"}
    for key, (lvl, title, detail) in conds.items():
        kind, _, ref = key.partition(":")
        if kind in ("pool", "poolspace", "quota"):  # straight to the pool's own page
            pool = ref.split(":")[0].split("/")[0]
            link = f"#pool:{urllib.parse.quote(pool, safe='')}" + ("/datasets" if kind == "quota" else "")
        else:
            link = links.get(kind, "#overview") + (urllib.parse.quote(ref, safe="") if kind == "drive" else "")
        items.append({"level": lvl, "title": title, "detail": detail, "link": link})
    for p in pools:
        ex = p.get("expand") or {}
        if ex.get("state") == "running":
            items.append({"level": "info", "title": f"Widening pool {p['name']}",
                          "detail": ", ".join(x for x in (f"{ex['percent']:.0f}% done" if ex.get("percent") is not None else "",
                                                          f"about {_eta(ex['eta'])} to go" if ex.get("eta") else "") if x),
                          "link": f"#pool:{urllib.parse.quote(p['name'], safe='')}"})
        sc = p.get("scan") or {}
        if p["name"] in case_pools or sc.get("state") not in ("running", "paused"):
            continue
        what = "Rebuilding" if sc.get("kind") == "resilver" else "Scrubbing"
        prog = f"{sc['percent']:.0f}% done" if sc.get("percent") is not None else ""
        items.append({"level": "info", "title": f"{what} pool {p['name']}" + (" (paused)" if sc["state"] == "paused" else ""),
                      "detail": ", ".join(x for x in (prog, f"about {_eta(sc.get('eta'))} to go" if sc.get("eta") and sc["state"] == "running" else "") if x),
                      "link": "#storage"})
    g = metrics.last_stall(30 * 60)
    if g:
        mins = (time.time() - g["end"]) / 60
        items.append({"level": "warn", "title": f"The server slowed down {'just now' if mins < 1 else f'{mins:.0f} min ago'}",
                      "detail": g["detail"], "link": "#overview", "action": "See graphs"})
    req, pkgs = upkeep.reboot_required()
    if req:
        items.append({"level": "info", "title": "Updates are waiting for a restart",
                      "detail": f"{len(pkgs)} package{'s' if len(pkgs) != 1 else ''}, including {', '.join(pkgs[:3])}" if pkgs else "",
                      "link": "#settings"})
    if tls.state.get("error") and tls.state.get("mode") == "tailscale":
        items.append({"level": "warn", "title": "The HTTPS certificate isn't renewing", "detail": tls.state["error"], "link": "#settings"})
    order = {"bad": 0, "warn": 1, "info": 2}
    items.sort(key=lambda i: order.get(i["level"], 3))
    data = {"items": items[:12], "more": max(0, len(items) - 12)}
    _attn.update(t=time.time(), data=data)
    return data


# --------------------------------------------------------------------------- misc
@app.get("/api/health")
def api_health():
    """For Docker's health check (healthcheck.py): answers as soon as the app is up, no sign-in needed."""
    return {"ok": True}


@app.get("/api/meta")
def api_meta():
    """What the page needs once at start: the title, the file browser's top folders (and which can be changed),
    whether sign-in is on, and the refresh rate."""
    roots = []
    if BROWSE_ROOT.exists():
        roots = sorted(e.name for e in BROWSE_ROOT.iterdir() if e.is_dir())
    cfg = load_config()
    root = BROWSE_ROOT.resolve() if BROWSE_ROOT.exists() else BROWSE_ROOT
    return {"title": cfg.get("title") or HOST_INFO["hostname"], "file_roots": roots, "config_error": cfg.get("_error"),
            "auth": security.enabled(), "refresh_ms": refresh_ms(), "file_write": fileops.WRITE,
            "file_writable": {r: _writable(root / r, root) for r in roots}}


@app.get("/")
def index(request: Request):
    """The dashboard page itself."""
    return security.page("index.html", request)


# --------------------------------------------------------------------------- the phone app (add to home screen)
def _icon_path():
    """The home-screen icon in use: yours from Settings, or the built-in one."""
    own = DATA_DIR / "app-icon.png"
    return own if own.is_file() else STATIC / "app-icon.png"


@app.get("/app-icon.png")
def app_icon():
    """The home-screen icon: yours if you've chosen one in Settings, otherwise the built-in one."""
    p = _icon_path()
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
def manifest():
    """The web app manifest, so 'Add to Home Screen' makes a proper app with the dashboard's name and icon."""
    title = load_config().get("title") or HOST_INFO.get("hostname") or "Homelab"
    v = int(_icon_path().stat().st_mtime)
    return JSONResponse({"name": f"{title} dashboard", "short_name": title[:12], "start_url": "/", "scope": "/",
                         "display": "standalone", "background_color": "#151b23", "theme_color": "#151b23",
                         "icons": [{"src": f"/app-icon.png?v={v}", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                                   {"src": f"/app-icon.png?v={v}", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]},
                        media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})


def _png_size(data: bytes):
    """(width, height) from a PNG's header, or None if it isn't a PNG."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


@app.put("/api/app-icon")
async def set_app_icon(request: Request):
    """A square PNG (the page makes one from whatever picture you choose), 180 to 1024 pixels."""
    data = await request.body()
    size = _png_size(data)
    if not size:
        raise HTTPException(400, "That isn't a PNG image")
    w, h = size
    if w != h or not 180 <= w <= 1024:
        raise HTTPException(400, "The icon needs to be square, between 180 and 1024 pixels")
    tmp = DATA_DIR / "app-icon.png.tmp"
    tmp.write_bytes(data)
    tmp.replace(DATA_DIR / "app-icon.png")
    upkeep.log("settings", "Changed the home-screen icon")
    return {"ok": True}


@app.delete("/api/app-icon")
def reset_app_icon():
    """Back to the built-in home-screen icon."""
    try:
        (DATA_DIR / "app-icon.png").unlink()
        upkeep.log("settings", "Went back to the built-in home-screen icon")
    except FileNotFoundError:
        pass
    return {"ok": True, "custom": False}


@app.get("/api/app-icon")
def app_icon_info():
    """Whether a home-screen icon of your own is set."""
    return {"custom": (DATA_DIR / "app-icon.png").is_file()}


FONT_RE = re.compile(r"^[a-z0-9-]+\.(woff2|txt)$")


@app.get("/assets/{name}")
def assets(name: str):
    """Bundled fonts (and their licences); no outside requests needed."""
    if not FONT_RE.fullmatch(name) or not (STATIC / "fonts" / name).is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(STATIC / "fonts" / name, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.exception_handler(StarletteHTTPException)
async def http_exc(request: Request, exc: StarletteHTTPException):
    """Errors come back as {"error": "..."} JSON. An unknown page address goes to the dashboard instead."""
    path = request.url.path
    # an unknown page address (e.g. /files instead of /#files) goes to the dashboard rather than an error
    if exc.status_code == 404 and request.method == "GET" and not path.startswith(("/api/", "/assets/")):
        return RedirectResponse("/", status_code=303)
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))


# --------------------------------------------------------------------------- the other modules
# Imported last: they use what's defined above (through M()), so this file has to be complete first.
import tls  # noqa: E402  (the HTTPS certificate)
import upkeep  # noqa: E402  (settings, restart schedule, alerts, clean-up, activity log)
import metrics  # noqa: E402  (GPU, power and history)
import drivecare  # noqa: E402  (replacing drives, scrubs, self-tests)
import zfsmgr  # noqa: E402  (pools, datasets, snapshots)
import fileops  # noqa: E402  (changing files from the file browser)
import passkeys  # noqa: E402  (signing in with Face ID, Touch ID, Windows Hello, a phone or a security key)
app.include_router(upkeep.router)
app.include_router(metrics.router)
app.include_router(drivecare.router)
app.include_router(zfsmgr.router)
app.include_router(fileops.router)
app.include_router(passkeys.router)


@app.on_event("startup")
def _start():
    """At start-up: the background threads that keep the readings fresh."""
    for fn in (system_sampler, container_poller, smart_poller):
        threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
    upkeep.start()
    metrics.start()
    drivecare.start()
    zfsmgr.start()
    fileops.start()
