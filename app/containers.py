"""
Containers: the list with live figures, their logs, and what you can do to them.

poller() runs in the background and keeps `state` fresh: every 3 s while a dashboard page is open (with CPU,
memory and network figures), otherwise every 10 s without them. Endpoints read from `state`, so a busy
Docker never slows a page down.

Docker's Python SDK is blocking, so endpoints that talk to Docker are plain `def`: FastAPI runs them in its
thread pool, and the event loop stays free.
"""
import contextvars
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

try:
    import docker
except ImportError:  # the dashboard still runs (without containers) if the package is missing
    docker = None

router = APIRouter()
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")  # colour codes in container logs


def M():
    """The main module (looked up when needed: importing it here would be circular)."""
    return sys.modules["main"]


def U():
    """The upkeep module: the activity log and alerts."""
    return sys.modules["upkeep"]


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


_dc = None
_dc_lock = threading.Lock()
state = {"items": [], "error": None, "updated": 0}  # the latest list, from poller()


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


_poll = {"prev": {}, "last_stats": 0.0, "stats": {}}  # what one pass leaves for the next
_wake = threading.Event()  # set after an action, so the list shows its effect straight away


def refresh_soon():
    """Ask the poller for a fresh pass now rather than at its next turn."""
    _wake.set()


def poll_once(viewing=True):
    """One pass: the container list with each one's details, and (while a page is open, or every 30 s) their
    CPU, memory and network figures. Updates `state`; raises if Docker can't be reached."""
    prev, stats = _poll["prev"], _poll["stats"]
    cl = dclient()
    cs = cl.containers.list(all=True, sparse=True)
    now = time.time()
    fresh = viewing or now - _poll["last_stats"] >= 30
    if fresh:
        running = [c for c in cs if c.attrs.get("State") == "running"]
        stats = _poll["stats"] = dict(zip([c.id for c in running], _stats_pool.map(_container_stats, running)))
        _poll["last_stats"] = now
    items = []
    for c in cs:
        try:
            a = _details(cl, c)
        except docker.errors.NotFound:
            continue  # removed between the list and the look (a compose re-create): skip, not a failed poll
        info = a.get("State") or {}
        labels = (a.get("Config") or {}).get("Labels") or {}
        st = dict(stats.get(c.id) or {}) if info.get("Running") else {}
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
        started = parse_docker_time(info.get("StartedAt"))
        nets = (a.get("NetworkSettings") or {}).get("Networks") or {}
        items.append({
            "id": c.id[:12],
            "name": (a.get("Name") or "").lstrip("/") or c.id[:12],
            "image": (a.get("Config") or {}).get("Image"),
            "image_id": a.get("Image"),  # the exact image it was made from (for update checks)
            "state": info.get("Status"),
            "health": (info.get("Health") or {}).get("Status"),
            "exit_code": info.get("ExitCode"),
            "started": started,
            "uptime": (now - started) if started and info.get("Running") else None,
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
    state.update(items=items, error=None, updated=now)


def poller():
    """Every 3 s while a dashboard page is open; otherwise every 10 s without CPU and memory figures
    (those every 30 s), which is all the crash and restart alerts need."""
    global _dc
    while True:
        viewing = time.time() - M()._viewing["t"] < 15
        try:
            poll_once(viewing)
        except Exception as e:
            state.update(error=f"{type(e).__name__}: {e}", updated=time.time())
            with _dc_lock:
                _dc = None  # Docker may have restarted: connect afresh next time
            time.sleep(5)
        _wake.wait(3 if viewing else 10)
        _wake.clear()


@router.get("/api/containers")
def api_containers():
    """Every container with its state and figures (from container_poller), and the names you've given them."""
    names = M().load_overrides().get("containers", {})
    return {**state, "items": [dict(c, display=names.get(c["name"]), is_self=bool(SELF_ID) and SELF_ID.startswith(c["id"]))
                                     for c in state["items"]]}


CID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")  # a container's id or name


def _get(cid):
    """A container by id or name, or a 404 that says why."""
    if not isinstance(cid, str) or not CID_RE.match(cid):
        raise HTTPException(400, "That isn't a container id or name")
    try:
        return dclient().containers.get(cid)
    except Exception as e:
        if docker is not None and isinstance(e, docker.errors.NotFound):
            raise HTTPException(404, f"There's no container called {cid}")
        raise HTTPException(503, f"Docker didn't answer: {e}")


@router.get("/api/containers/{cid}/logs")
def api_container_logs(cid: str, tail: int = Query(300, ge=10, le=5000), since: int = Query(None, ge=1, le=90 * 86400),
                       q: str = Query(None, max_length=200)):
    """A container's log as plain text, with colour codes taken out: the last `tail` lines, optionally only from
    the last `since` seconds, and only lines containing `q` (any case). Searching looks through up to 20,000
    lines, so it finds things further back than the lines shown."""
    c = _get(cid)
    kw = {"timestamps": True, "tail": 20_000 if q else tail}
    if since:
        kw["since"] = int(time.time()) - since
    try:
        raw = c.logs(**kw)
    except Exception as e:
        raise HTTPException(503, f"Couldn't read the log: {getattr(e, 'explanation', None) or e}")
    lines = ANSI_RE.sub("", raw.decode("utf-8", "replace")).splitlines()
    if q:
        needle = q.casefold()
        lines = [ln for ln in lines if needle in ln.casefold()][-tail:]
    return PlainTextResponse("\n".join(lines) + ("\n" if lines else ""))


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

# action -> (words for the activity log and messages, whether it needs the password again)
ACTIONS = {
    "start":   ("Started",   "starting",   False),
    "stop":    ("Stopped",   "stopping",   False),
    "restart": ("Restarted", "restarting", False),
    "pause":   ("Paused",    "pausing",    False),
    "unpause": ("Resumed",   "resuming",   False),
    "remove":  ("Removed",   "removing",   True),
}
BULK_MAX = 100


class ActionRefused(Exception):
    """An action that can't be done to this container, with the reason in words."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _display(c):
    return M().load_overrides().get("containers", {}).get(c.name) or c.name


def act(c, action, force=False):
    """Do one action to one container. Returns a short result; raises ActionRefused with a reason.
    - LabKeel can restart itself (it answers first, then goes) but never stops, pauses or removes itself,
      or there'd be nothing left to undo it with.
    - Stop and restart give the container 30 s to shut down cleanly (game servers save their world).
    - Remove only removes a stopped container unless `force`, in which case it's stopped first. Its volumes
      are kept. A container from a compose project comes back the next time that project is started."""
    name = _display(c)
    is_self = bool(SELF_ID) and c.id == SELF_ID
    if is_self and action != "restart" and action != "start":
        raise ActionRefused(409, f"LabKeel can't {action} itself, or there'd be no way to undo it from here.")
    if is_self and action == "restart":
        U().log("container", f"Restarted {name} (LabKeel itself)")
        threading.Timer(1.0, lambda: c.restart(timeout=10)).start()  # answer first, then go
        return {"self": True}
    with _action_lock:
        if c.id in _busy:
            raise ActionRefused(409, f"{name} is already busy with something else. Try again in a moment.")
        _busy.add(c.id)
    try:
        status = (c.attrs.get("State") or {}).get("Status") if isinstance(c.attrs.get("State"), dict) else c.status
        if action == "remove" and status in ("running", "paused", "restarting") and not force:
            raise ActionRefused(409, f"{name} is still running. Stop it first, or remove it anyway.")
        if action == "pause" and status != "running":
            raise ActionRefused(409, f"{name} isn't running, so there's nothing to pause.")
        if action == "unpause" and status != "paused":
            raise ActionRefused(409, f"{name} isn't paused.")
        try:
            if action == "start":
                c.start()
            elif action in ("stop", "restart"):
                getattr(c, action)(timeout=30)
            elif action in ("pause", "unpause"):
                getattr(c, action)()
            elif action == "remove":
                c.remove(force=force, v=False)
        except Exception as e:
            msg = getattr(e, "explanation", None) or str(e)
            U().log("error", f"Couldn't {action} {name}: {msg}")
            raise ActionRefused(502, f"Docker couldn't {action} {name}: {msg}")
    finally:
        with _action_lock:
            _busy.discard(c.id)
    refresh_soon()
    if action in ("start", "stop", "remove"):
        U().mark_stopped_on_purpose(c.name, action != "start")
    U().log("container", f"{ACTIONS[action][0]} {name}")
    return {}


def _check_action(action, request, body):
    if action not in ACTIONS:
        raise HTTPException(400, "The action must be one of: " + ", ".join(ACTIONS))
    if ACTIONS[action][2]:
        sys.modules["security"].confirm_password(request, body.get("password"))


@router.post("/api/containers/{cid}/action")
def api_container_action(cid: str, request: Request, body: dict = Body(...)):
    """Start, stop, restart, pause, resume ("unpause") or remove one container. Removing asks for the password."""
    action = body.get("action")
    _check_action(action, request, body)
    c = _get(cid)
    try:
        out = act(c, action, force=bool(body.get("force")))
    except ActionRefused as e:
        raise HTTPException(e.status, str(e))
    return {"ok": True, **out}


@router.post("/api/containers/bulk")
def api_containers_bulk(request: Request, body: dict = Body(...)):
    """The same action on several containers (up to 100) at once, four at a time. Each one succeeds or fails on
    its own; the answer lists what happened to each. LabKeel itself is always left alone here."""
    action, ids = body.get("action"), body.get("ids")
    if not isinstance(ids, list) or not ids or len(ids) > BULK_MAX or not all(isinstance(i, str) for i in ids):
        raise HTTPException(400, f"Choose between 1 and {BULK_MAX} containers")
    _check_action(action, request, body)
    force = bool(body.get("force"))

    def one(cid):
        try:
            c = _get(cid)
            if SELF_ID and c.id == SELF_ID:
                return {"id": cid, "ok": False, "error": "Skipped: that's LabKeel itself."}
            act(c, action, force=force)
            return {"id": cid, "ok": True}
        except (HTTPException, ActionRefused) as e:
            return {"id": cid, "ok": False, "error": getattr(e, "detail", None) or str(e)}

    # Worker threads don't inherit this request's context (who's signed in, for the activity log), so each
    # job runs in its own copy of it.
    ctx = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="ctr-bulk") as ex:
        results = list(ex.map(lambda cid: ctx.copy().run(one, cid), list(dict.fromkeys(ids))))
    done = sum(r["ok"] for r in results)
    return {"ok": done == len(results), "done": done, "results": results}
