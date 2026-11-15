"""
The Clusters page: machines looked after together, and Docker Swarm for running services across them.

Two kinds of cluster, and each can be used without the other:

  Group of servers   Just a name for machines LabKeel already knows (this one and its agents). It shows them side by
                     side, with their load and containers. Nothing is installed or changed on them, so any admin can
                     make one. Kept in data/clusters.json.
  Docker Swarm       Docker's own clustering. LabKeel talks to Docker on this machine, which has to be a manager, and
                     shows the nodes, the services and whether the managers can still agree (quorum).
                     Kubernetes isn't here yet: the page shows it as "later".

Looking is open to everyone who can sign in. Changing a Swarm (starting one, joining a machine, draining a node,
adding, scaling, restarting or removing a service, leaving) is off unless LABKEEL_ALLOW_CLUSTER_CHANGES=1, admins
only, asks for the password again, and every step is in the activity log. A join token is only ever sent to an admin
who has just given the password, and is never stored or logged.
"""
import ipaddress
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, HTTPException, Request

import containers
import env

router = APIRouter()

ALLOWED = env.flag("LABKEEL_ALLOW_CLUSTER_CHANGES")
MAX_GROUPS = 20
MAX_MEMBERS = 100
MAX_COPIES = 50         # most copies of one service LabKeel will ask for
SWARM_PORT = 2377       # where Swarm's managers listen (other machines join through it)
CACHE_FOR = 4           # seconds Docker's answer is reused, so several open pages cost one set of questions
EVENTS_SHOWN = 20

_lock = threading.Lock()
_cache = {"t": 0.0, "data": None}


def M():
    return sys.modules["main"]


def U():
    return sys.modules["upkeep"]


def S():
    return sys.modules["security"]


NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")
SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,199}$")
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(:\d{1,5})?$")
GROUP_RE = re.compile(r"^[0-9a-f]{12}$")
DOCKER_ID_RE = re.compile(r"^[a-z0-9]{10,40}$")


def why_not():
    """Why a Swarm can't be changed from here, or None."""
    if not ALLOWED:
        return ("Changing a Swarm is off. Add LABKEEL_ALLOW_CLUSTER_CHANGES=1 to LabKeel's docker-compose.yml and run "
                "docker compose up -d to turn it on.")
    return None


# --------------------------------------------------------------------------- stored groups
def _load():
    d = U()._read("clusters.json", {})
    if not isinstance(d, dict):
        d = {}
    g = d.get("groups")
    d["groups"] = {k: v for k, v in g.items() if isinstance(v, dict)} if isinstance(g, dict) else {}   # a hand-edited file
    return d


def _save(d):
    U()._write("clusters.json", d)


def _members(g):
    return [s for s in (g.get("servers") if isinstance(g, dict) else None) or [] if isinstance(s, str)]


def swarm_name():
    n = _load().get("swarm_name")
    return n if isinstance(n, str) and NAME_RE.match(n) else "Docker Swarm"


def forget(sid):
    """A server was removed from LabKeel: take it out of every group."""
    with _lock:
        d = _load()
        changed = False
        for g in d["groups"].values():
            if sid in _members(g):
                g["servers"] = [s for s in _members(g) if s != sid]
                changed = True
        if changed:
            _save(d)


def _name(v):
    v = str(v or "").strip()
    if not NAME_RE.match(v):
        raise HTTPException(400, "Give it a name: letters, numbers, spaces, dots and dashes, up to 40.")
    return v


# --------------------------------------------------------------------------- the machines LabKeel knows
def _machines():
    """Every machine as a small dict, by id: this one ("this") and each agent."""
    sv = sys.modules["servers"]
    items, _ = sv.listing()
    m = M()
    with m._sys_lock:
        cur = dict(m._sys.get("current") or {})
    own = sv.summarise({"system": cur, "containers": containers.state.get("items") or [], "host": m.HOST_INFO})
    out = {"this": {"id": "this", "name": m.HOST_INFO.get("hostname") or "This server", "state": "online",
                    "agent": False, "problems": 0, **_figures(own)}}
    for s in items:
        out[s["id"]] = {"id": s["id"], "name": s["name"], "state": s["state"], "agent": True,
                        "problems": s.get("problems") or 0, **_figures(s)}
    return out


def _figures(s):
    return {k: s.get(k) for k in ("cpu", "memory", "running", "total")}


# --------------------------------------------------------------------------- groups
def _group_card(gid, g, machines):
    nodes = [machines[s] for s in _members(g) if s in machines]
    off = [n for n in nodes if n["state"] != "online"]
    trouble = sum(n["problems"] for n in nodes)
    if not nodes:
        status, note = "empty", "No machines in it yet. Open it to choose some."
    elif off:
        status = "attention"
        names = [n["name"] for n in off]
        note = f"{_join(names)} {'isn' if len(names) == 1 else 'aren'}'t reporting."
    elif trouble:
        status, note = "attention", f"{trouble} problem{'s' if trouble != 1 else ''} on its machines."
    else:
        status, note = "healthy", None
    return {"id": gid, "kind": "group", "name": g["name"] if isinstance(g.get("name"), str) and g["name"] else "Group", "status": status, "note": note,
            "nodes": nodes, "problems": trouble,
            "containers": {"running": sum(n["running"] or 0 for n in nodes), "total": sum(n["total"] or 0 for n in nodes)}}


def _join(names):
    names = list(names)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _group_events(g, machines):
    """Problems on a group's machines, from the same checks as the alerts (this machine's own are on the Overview)."""
    ids = set(_members(g))
    out = []
    for key, (level, title, detail) in sys.modules["servers"].conditions(None).items():
        if key.split(":")[1] in ids:
            out.append({"level": level, "title": title, "detail": detail})
    out.sort(key=lambda e: (e["level"] != "bad", e["title"]))
    return out


# --------------------------------------------------------------------------- Docker Swarm: reading
_TS = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)$")


def _epoch(s):
    """A Docker timestamp as seconds since 1970, or None (Docker's 'never' is the year 1)."""
    m = _TS.match(s or "") if isinstance(s, str) else None
    if not m:
        return None
    y, mo, d, h, mi, se, tz = m.groups()
    try:
        t = datetime(int(y), int(mo), int(d), int(h), int(mi), int(se), tzinfo=timezone.utc)
        if tz != "Z":
            t -= timedelta(hours=int(tz[1:3]), minutes=int(tz[4:6])) * (1 if tz[0] == "+" else -1)
    except ValueError:
        return None
    t = t.timestamp()
    return t if t > 0 else None


def _why(e):
    """Docker's own explanation of what went wrong, short enough for a banner."""
    t = str(getattr(e, "explanation", None) or e).strip().replace("\n", " ")
    return (t[:200].rstrip(" .,;:") or "Docker didn't say why") + "."


def _image(spec):
    return re.sub(r"@sha256:[0-9a-f]+$", "", str(((spec.get("TaskTemplate") or {}).get("ContainerSpec") or {}).get("Image") or ""))


def _pin(spec, names):
    """The machine a service is pinned to by a placement rule, and whether it has any other rules."""
    pinned, other = None, False
    for c in ((spec.get("TaskTemplate") or {}).get("Placement") or {}).get("Constraints") or []:
        m = re.match(r"^\s*node\.(hostname|id)\s*==\s*(\S+)\s*$", str(c))
        if m and pinned is None:
            pinned = m.group(2) if m.group(1) == "hostname" else names.get(m.group(2), m.group(2))
        else:
            other = True
    return pinned, other


def _copies(n):
    return f"{n} cop{'y' if n == 1 else 'ies'}"


def _shape(info, nodes, services, tasks, machines, now=None):
    """What the page shows, from Docker's answers (separate from asking, so it can be tried with made-up ones)."""
    now = now or time.time()
    by_name = {m["name"].lower(): m for m in machines.values()}
    names = {n.get("ID"): ((n.get("Description") or {}).get("Hostname") or n.get("ID", "")[:12]) for n in nodes}
    running = [t for t in tasks if t.get("DesiredState") == "running" and (t.get("Status") or {}).get("State") == "running"]
    on_node, on_service = {}, {}
    for t in running:
        on_node[t.get("NodeID")] = on_node.get(t.get("NodeID"), 0) + 1
        on_service.setdefault(t.get("ServiceID"), []).append(t)

    out_nodes = []
    for n in nodes:
        spec, st, ms = n.get("Spec") or {}, n.get("Status") or {}, n.get("ManagerStatus") or {}
        name = names[n.get("ID")]
        m = by_name.get(name.lower())
        state = st.get("State") or "unknown"
        out_nodes.append({
            "id": n.get("ID"), "name": name, "role": spec.get("Role") or "worker", "leader": bool(ms.get("Leader")),
            "reachable": (ms.get("Reachability") == "reachable") if ms else None,
            "state": state, "availability": spec.get("Availability") or "active", "addr": st.get("Addr"),
            "tasks": on_node.get(n.get("ID"), 0), "cpu": m and m["cpu"], "memory": m and m["memory"],
            "since": _epoch(n.get("UpdatedAt")) if state != "ready" else None,
            "this": n.get("ID") == (info or {}).get("NodeID"),
            "engine": ((n.get("Description") or {}).get("Engine") or {}).get("EngineVersion"),
        })
    out_nodes.sort(key=lambda n: (n["role"] != "manager", n["name"].lower()))

    wanted_global = sum(1 for n in out_nodes if n["availability"] == "active")
    out_services = []
    for s in services:
        spec = s.get("Spec") or {}
        mode = "global" if "Global" in (spec.get("Mode") or {}) else "replicated"
        wanted = wanted_global if mode == "global" else int(((spec.get("Mode") or {}).get("Replicated") or {}).get("Replicas") or 0)
        mine = on_service.get(s.get("ID"), [])
        up = len(mine)
        pinned, other = _pin(spec, names)
        if mode == "global":
            rule = "One on every machine" if not (pinned or other) else "One on each machine that fits its rules"
        else:
            rule = _copies(wanted) + (f", pinned to {pinned}" if pinned else ", restricted by placement rules" if other else ", any machine")
        out_services.append({
            "id": s.get("ID"), "name": spec.get("Name") or "", "image": _image(spec), "mode": mode, "wanted": wanted,
            "running": up, "pinned": pinned, "rule": rule,
            "state": "stopped" if wanted == 0 else "ok" if up >= wanted else "warn" if up else "bad",
            "where": sorted({names.get(t.get("NodeID"), "?") for t in mine}, key=str.lower),
            "ports": [{"published": p.get("PublishedPort"), "target": p.get("TargetPort"), "protocol": p.get("Protocol")}
                      for p in ((s.get("Endpoint") or {}).get("Ports") or []) if p.get("PublishedPort")],
        })
    out_services.sort(key=lambda s: s["name"].lower())

    svc_names = {s.get("ID"): (s.get("Spec") or {}).get("Name") for s in services}
    bad = [t for t in tasks if (t.get("Status") or {}).get("State") in ("failed", "rejected") and t.get("ServiceID") in svc_names]
    bad.sort(key=lambda t: _epoch((t.get("Status") or {}).get("Timestamp")) or 0, reverse=True)
    events = [{"time": _epoch((t.get("Status") or {}).get("Timestamp")), "service": svc_names.get(t.get("ServiceID")),
               "node": names.get(t.get("NodeID")), "state": (t.get("Status") or {}).get("State"),
               "message": str(((t.get("Status") or {}).get("Err") or (t.get("Status") or {}).get("Message") or ""))[:300]}
              for t in bad[:EVENTS_SHOWN]]

    quorum = _quorum(out_nodes)
    down = [n for n in out_nodes if n["state"] != "ready"]
    short = [s for s in out_services if s["state"] in ("warn", "bad")]
    if quorum["state"] == "lost":
        status = "down"
    elif down or short:
        status = "attention"
    else:
        status = "healthy"
    note = None
    if down:
        who = _join(n["name"] for n in down) if len(down) <= 3 else f"{len(down)} machines"
        note = f"{who} {'is' if len(down) == 1 else 'are'} down."
        if any(n["role"] == "manager" for n in down) and quorum["state"] != "lost":
            note += f" The cluster still works: {quorum['up']} of {quorum['managers']} managers agree."
            if quorum["state"] == "none":
                note += " One more failure would stop it making changes."
    if quorum["state"] == "lost":
        note = (note + " " if note else "") + quorum["text"] + " " + quorum["detail"]
    elif short and not down:
        note = f"{len(short)} service{'s' if len(short) != 1 else ''} below {'its' if len(short) == 1 else 'their'} wanted copies."
    return {"nodes": out_nodes, "services": out_services, "quorum": quorum, "events": events, "status": status,
            "note": note, "addr": (info or {}).get("NodeAddr"),
            "totals": {"services": len(out_services), "full": sum(1 for s in out_services if s["state"] in ("ok", "stopped"))}}


def _quorum(nodes):
    """Whether the managers can still agree: more than half of them have to be up."""
    mgrs = [n for n in nodes if n["role"] == "manager"]
    total = len(mgrs)
    up = sum(1 for n in mgrs if n["state"] == "ready" and n["reachable"] is not False)
    need = total // 2 + 1
    spare = up - need
    q = {"managers": total, "up": up, "needed": need, "spare": spare}
    if up < need:
        q.update(state="lost", text=f"{up} of {total} managers are up. It needs {need}.",
                 detail="It can't accept changes until one comes back. Running services keep running.")
    elif total == 1:
        q.update(state="single", text="One manager, and it's up.",
                 detail="If it goes down, nothing can be changed until it's back. Running services keep running. "
                        "Three managers is the smallest number that can survive a failure.")
    elif total == 2:
        q.update(state="none", text=f"{up} of {total} managers are up. It needs {need}.",
                 detail="Two managers can't protect each other: if either goes down, changes stop. "
                        "Three managers is the smallest number that can survive a failure.")
    elif spare == 0:
        q.update(state="none", text=f"{up} of {total} managers are up. It needs {need}.",
                 detail="It still works, but with no spare. If another manager goes down, it stops accepting changes "
                        "until one comes back. Running services keep running.")
    else:
        q.update(state="ok", text=f"{up} of {total} managers are up. It needs {need}.",
                 detail=f"It can lose {spare} more manager{'s' if spare != 1 else ''} and carry on.")
    return q


def _read_swarm(machines):
    """Ask Docker. {"state": unavailable | off | worker | error | manager, ...}; a manager's also has the shaped answer in it,
    and "_raw" (Docker's own lists, for the drain preview; never sent to the page)."""
    try:
        cl = containers.dclient()
        info = cl.info().get("Swarm") or {}
    except Exception as e:
        return {"state": "unavailable", "message": f"LabKeel couldn't ask Docker about Swarm. {_why(e)}"}
    local = info.get("LocalNodeState") or "inactive"
    if local == "inactive":
        return {"state": "off"}
    if local in ("pending", "error", "locked"):
        why = {"locked": "Swarm is locked: unlock it with docker swarm unlock on this machine.",
               "pending": "Swarm is still starting on this machine."}.get(local) or f"Swarm has a problem on this machine. {info.get('Error') or ''}".strip()
        return {"state": "error", "message": why}
    if not info.get("ControlAvailable"):
        return {"state": "worker",
                "message": "This machine is a worker in a Swarm. LabKeel can only show the cluster from a manager."}
    try:
        nodes = [n.attrs for n in cl.nodes.list()]
        services = [s.attrs for s in cl.services.list()]
        tasks = cl.api.tasks()
    except Exception as e:
        return {"state": "error", "message": f"Docker wouldn't list the Swarm. {_why(e)}"}
    out = _shape(info, nodes, services, tasks, machines)
    out.update(state="manager", _raw={"nodes": nodes, "services": services, "tasks": tasks})
    return out


def swarm(machines=None, fresh=False):
    with _lock:
        c = _cache["data"]
        if c is not None and not fresh and time.time() - _cache["t"] < CACHE_FOR:
            return c
    data = _read_swarm(machines or _machines())
    with _lock:
        _cache.update(t=time.time(), data=data)
    return data


def _changed():
    with _lock:
        _cache.update(t=0.0, data=None)


def _public(sw):
    return {k: v for k, v in sw.items() if not k.startswith("_")}


def _swarm_card(sw):
    base = {"id": "swarm", "kind": "swarm", "name": swarm_name()}
    if sw["state"] == "manager":
        return {**base, **{k: sw[k] for k in ("status", "note", "nodes", "quorum", "totals")}}
    return {**base, "status": "limited" if sw["state"] == "worker" else "attention", "note": sw.get("message"),
            "nodes": [], "quorum": None, "totals": None}


# --------------------------------------------------------------------------- endpoints: looking
@router.get("/api/clusters")
def api_clusters(fresh: bool = False):
    """Every cluster as a card, and the figures along the top."""
    machines = _machines()
    d = _load()
    clusters = [_group_card(gid, g, machines) for gid, g in d["groups"].items()]
    clusters.sort(key=lambda c: c["name"].lower())
    sw = swarm(machines, fresh)
    if sw["state"] not in ("off", "unavailable"):
        clusters.append(_swarm_card(sw))
    seen = {}
    for c in clusters:
        for n in c["nodes"]:
            up = n["state"] in ("online", "ready")
            seen[n["name"].lower()] = seen.get(n["name"].lower(), False) or up
    totals = (sw.get("totals") if sw["state"] == "manager" else None) or None
    return {
        "clusters": clusters,
        "tiles": {"healthy": sum(1 for c in clusters if c["status"] == "healthy"),
                  "attention": sum(1 for c in clusters if c["status"] in ("attention", "down")),
                  "machines": len(seen), "online": sum(1 for v in seen.values() if v), "services": totals},
        "swarm": {"state": sw["state"], "message": sw.get("message")},
        "changes": ALLOWED, "why_not": why_not(),
        "machines": [{"id": m["id"], "name": m["name"]} for m in sorted(machines.values(), key=lambda m: m["name"].lower())],
        "kubernetes": "later",
    }


@router.get("/api/clusters/{cid}")
def api_cluster(cid: str, fresh: bool = False):
    """One cluster for its page: a group's machines and problems, or a Swarm's nodes, services and events."""
    machines = _machines()
    if cid == "swarm":
        sw = swarm(machines, fresh)
        if sw["state"] != "manager":
            raise HTTPException(404, sw.get("message") or "Swarm isn't running on this machine.")
        return {**_public(sw), **_swarm_card(sw), "changes": ALLOWED, "why_not": why_not(),
                "address": f"{sw['addr']}:{SWARM_PORT}" if sw.get("addr") else None}
    g = _load()["groups"].get(cid) if GROUP_RE.match(cid) else None
    if not isinstance(g, dict):
        raise HTTPException(404, "There's no cluster with that name here.")
    return {**_group_card(cid, g, machines), "members": [s for s in _members(g) if s in machines],
            "events": _group_events(g, machines),
            "machines": [{"id": m["id"], "name": m["name"]} for m in sorted(machines.values(), key=lambda m: m["name"].lower())]}


# --------------------------------------------------------------------------- endpoints: groups (a label, so no gate)
def _ids(v, machines):
    if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
        raise HTTPException(400, "Choose which machines are in it.")
    ids = list(dict.fromkeys(v))
    if len(ids) > MAX_MEMBERS:
        raise HTTPException(400, f"A group can have up to {MAX_MEMBERS} machines.")
    missing = [x for x in ids if x not in machines]
    if missing:
        raise HTTPException(400, "One of those machines isn't known to LabKeel any more. Refresh the page and choose again.")
    return ids


def _unique(d, name, ignore=None):
    if any(str(g.get("name") or "").lower() == name.lower() for gid, g in d["groups"].items() if gid != ignore) or (
            ignore != "swarm" and name.lower() == str(d.get("swarm_name") or "").lower()):
        raise HTTPException(409, f"There's already a cluster called {name}. Choose another name.")


@router.post("/api/clusters/groups")
def api_group_add(request: Request, body: dict = Body(...)):
    S().require_admin(request)
    name = _name(body.get("name"))
    ids = _ids(body.get("servers", []), _machines())
    with _lock:
        d = _load()
        if len(d["groups"]) >= MAX_GROUPS:
            raise HTTPException(409, f"That's as many groups as LabKeel keeps ({MAX_GROUPS}).")
        _unique(d, name)
        gid = secrets.token_hex(6)
        d["groups"][gid] = {"name": name, "servers": ids, "created": int(time.time())}
        _save(d)
    U().log("settings", f"Made the cluster {name} (a group of {len(ids)} server{'s' if len(ids) != 1 else ''})")
    return {"ok": True, "id": gid}


@router.post("/api/clusters/groups/{gid}")
def api_group_change(gid: str, request: Request, body: dict = Body(...)):
    S().require_admin(request)
    with _lock:
        d = _load()
        g = d["groups"].get(gid) if GROUP_RE.match(gid) else None
        if not isinstance(g, dict):
            raise HTTPException(404, "There's no cluster with that name here.")
        if "name" in body:
            name = _name(body["name"])
            _unique(d, name, gid)
        else:
            name = g.get("name")
        ids = _ids(body["servers"], _machines()) if "servers" in body else _members(g)
        g.update(name=name, servers=ids)
        _save(d)
    U().log("settings", f"Changed the cluster {name}")
    return {"ok": True}


@router.post("/api/clusters/groups/{gid}/delete")
def api_group_delete(gid: str, request: Request):
    """Forgets the group. The machines in it are untouched."""
    S().require_admin(request)
    with _lock:
        d = _load()
        g = d["groups"].pop(gid, None) if GROUP_RE.match(gid) else None
        if not isinstance(g, dict):
            raise HTTPException(404, "There's no cluster with that name here.")
        _save(d)
    U().log("settings", f"Deleted the cluster {g.get('name')} (its machines are untouched)")
    return {"ok": True}


# --------------------------------------------------------------------------- endpoints: Swarm changes
def _gate(request, body):
    """Everything a Swarm change needs first: an admin, the setting on, and the password again."""
    S().require_admin(request)
    w = why_not()
    if w:
        raise HTTPException(409, w)
    S().confirm_password(request, body.get("password"))


def _manager():
    """The Docker client, once this machine is known to be a Swarm manager."""
    sw = swarm()
    if sw["state"] != "manager":
        raise HTTPException(409, sw.get("message") or "Swarm isn't running on this machine. Start one from Clusters first.")
    return containers.dclient()


def _docker(what, fn, *a, **kw):
    """Run a Docker call, and say what Docker said if it refuses."""
    try:
        return fn(*a, **kw)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(409, f"Docker wouldn't {what}. {_why(e)}")


def _node_id(nid):
    if not DOCKER_ID_RE.match(nid or ""):
        raise HTTPException(404, "There's no such machine in the Swarm.")
    return nid


def _find(kind, ident):
    sw = swarm()
    if sw["state"] != "manager":
        raise HTTPException(409, sw.get("message") or "Swarm isn't running on this machine.")
    for x in sw[kind]:
        if x["id"] == ident:
            return x
    raise HTTPException(404, "That's no longer in the Swarm. Refresh the page.")


@router.post("/api/clusters/swarm/init")
def api_init(request: Request, body: dict = Body(...)):
    """Start a Swarm on this machine (it becomes its first manager)."""
    S().require_admin(request)
    name = _name(body.get("name") or "Docker Swarm")
    addr = str(body.get("address") or "").strip()
    if addr:
        try:
            ipaddress.ip_address(addr.strip("[]"))
        except ValueError:
            if not HOST_RE.match(addr):
                raise HTTPException(400, "That address isn't one LabKeel can use. Give an IP address or a name, like 192.168.1.10.")
    _unique(_load(), name, "swarm")
    _gate(request, body)
    if swarm(fresh=True)["state"] != "off":
        raise HTTPException(409, "Swarm is already on for this machine.")
    cl = containers.dclient()
    try:
        cl.swarm.init(advertise_addr=addr or None, listen_addr=f"0.0.0.0:{SWARM_PORT}")
    except Exception as e:
        hint = " Type this machine's own address under Address, and try again." if "advertise" in str(e).lower() else ""
        raise HTTPException(409, f"Docker wouldn't start a Swarm. {_why(e)}{hint}")
    with _lock:
        d = _load()
        d["swarm_name"] = name
        _save(d)
    _changed()
    U().log("container", f"Started a Docker Swarm called {name} on this machine")
    return {"ok": True}


@router.post("/api/clusters/swarm/rename")
def api_swarm_rename(request: Request, body: dict = Body(...)):
    """What the page calls the Swarm (Docker's own name for it is always 'default')."""
    S().require_admin(request)
    name = _name(body.get("name"))
    with _lock:
        d = _load()
        _unique(d, name, "swarm")
        d["swarm_name"] = name
        _save(d)
    U().log("settings", f"Renamed the Swarm to {name}")
    return {"ok": True}


@router.post("/api/clusters/swarm/token")
def api_token(request: Request, body: dict = Body(...)):
    """The command to run on another machine to join this Swarm. Sent once, to an admin who has just given the
    password; the token isn't stored here and isn't in the activity log."""
    role = body.get("role", "worker")
    if role not in ("worker", "manager"):
        raise HTTPException(400, "Choose worker or manager.")
    _gate(request, body)
    cl = _manager()
    sw = swarm()
    if not sw.get("addr"):
        raise HTTPException(409, "Docker didn't say which address this machine's Swarm uses, so there's no command to give.")

    def tokens():
        cl.swarm.reload()
        return cl.swarm.attrs.get("JoinTokens") or {}

    token = _docker("give the join token", tokens).get("Manager" if role == "manager" else "Worker")
    if not token:
        raise HTTPException(409, "Docker has no join token to give.")
    U().log("container", f"Showed the Swarm join command for a {role}")
    return {"role": role, "token": token, "address": f"{sw['addr']}:{SWARM_PORT}",
            "command": f"docker swarm join --token {token} {sw['addr']}:{SWARM_PORT}"}


@router.get("/api/clusters/swarm/nodes/{nid}/preview")
def api_drain_preview(nid: str):
    """What draining a machine would do to each service running on it (nothing is changed)."""
    _node_id(nid)
    node = _find("nodes", nid)
    _manager()
    raw = swarm()["_raw"]
    names = {n["ID"]: ((n.get("Description") or {}).get("Hostname") or n["ID"][:12]) for n in raw["nodes"]}
    others = [n for n in swarm()["nodes"] if n["id"] != nid and n["state"] == "ready" and n["availability"] == "active"]
    svc = {s["ID"]: s for s in raw["services"]}
    counts = {}
    for t in raw["tasks"]:
        if t.get("NodeID") == nid and t.get("DesiredState") == "running" and (t.get("Status") or {}).get("State") == "running" and t.get("ServiceID") in svc:
            counts[t["ServiceID"]] = counts.get(t["ServiceID"], 0) + 1
    rows = []
    for sid, k in counts.items():
        spec = svc[sid].get("Spec") or {}
        name = spec.get("Name") or sid[:12]
        pinned, other = _pin(spec, names)
        if "Global" in (spec.get("Mode") or {}):
            row = {"outcome": "stops", "text": "Stops (one per machine)"}
        elif pinned and pinned.lower() == node["name"].lower():
            row = {"outcome": "stuck", "text": "Can't move: pinned here"}
        elif not others:
            row = {"outcome": "stuck", "text": "Can't move: no other machine is ready"}
        elif len(others) == 1 and not other:
            row = {"outcome": "moves", "text": f"To {others[0]['name']}"}
        else:
            row = {"outcome": "moves", "text": "To another machine" + (" that fits its rules" if other else "")}
        rows.append({"service": name, "copies": k, **row})
    rows.sort(key=lambda r: r["service"].lower())
    return {"node": node["name"], "availability": node["availability"], "rows": rows,
            "stops": [r["service"] for r in rows if r["outcome"] == "stuck"]}


@router.post("/api/clusters/swarm/nodes/{nid}/availability")
def api_availability(nid: str, request: Request, body: dict = Body(...)):
    """Drain a machine (services leave it) or make it active again."""
    _node_id(nid)
    a = body.get("availability")
    if a not in ("active", "drain"):
        raise HTTPException(400, "Choose active or drain.")
    node = _find("nodes", nid)
    _gate(request, body)
    cl = _manager()

    def go():
        n = cl.nodes.get(nid)
        spec = dict(n.attrs.get("Spec") or {})
        spec["Availability"] = a
        n.update(spec)

    _docker("change that machine", go)
    _changed()
    U().log("container", f"{'Drained' if a == 'drain' else 'Made active again'} {node['name']} in the Swarm")
    return {"ok": True}


@router.post("/api/clusters/swarm/nodes/{nid}/remove")
def api_node_remove(nid: str, request: Request, body: dict = Body(default={})):
    """Take a machine that's down out of the Swarm (Docker refuses while it's ready)."""
    _node_id(nid)
    node = _find("nodes", nid)
    if node["state"] == "ready":
        raise HTTPException(409, f"{node['name']} is still ready. Only a machine that's down can be removed here; "
                                 "on that machine, run docker swarm leave.")
    _gate(request, body)
    cl = _manager()
    _docker("remove that machine", lambda: cl.nodes.get(nid).remove(force=False))
    _changed()
    U().log("container", f"Removed {node['name']} from the Swarm")
    return {"ok": True}


def _copies_in(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = -1
    if isinstance(v, bool) or not 0 <= n <= MAX_COPIES:
        raise HTTPException(400, f"Choose how many copies, from 0 to {MAX_COPIES}.")
    return n


def _port_in(v, what):
    if v in (None, ""):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = 0
    if isinstance(v, bool) or not 1 <= n <= 65535:
        raise HTTPException(400, f"{what} should be a number from 1 to 65535.")
    return n


@router.post("/api/clusters/swarm/services")
def api_service_add(request: Request, body: dict = Body(...)):
    """Run an image as a service. Kept simple on purpose: no folders from the machine and no environment variables
    (a Swarm service can't be privileged either), so it can't reach the host."""
    name, image = str(body.get("name") or "").strip(), str(body.get("image") or "").strip()
    if not SERVICE_RE.match(name):
        raise HTTPException(400, "Name the service with letters, numbers, dots, dashes and underscores, up to 63.")
    if not IMAGE_RE.match(image):
        raise HTTPException(400, "That doesn't look like an image name. Something like nginx:alpine or ghcr.io/owner/app:1.2.")
    mode = body.get("mode", "replicated")
    if mode not in ("replicated", "global"):
        raise HTTPException(400, "Choose copies, or one on every machine.")
    copies = _copies_in(body.get("copies", 1)) if mode == "replicated" else None
    published, target = _port_in(body.get("published"), "The port"), _port_in(body.get("target"), "The app's port")
    if (published is None) != (target is None):
        published, target = (published or target), (target or published)   # one number given: the same on both sides
    _find_free(name)
    _gate(request, body)
    cl = _manager()
    from docker.types import EndpointSpec, ServiceMode
    kw = {"name": name, "mode": ServiceMode("global") if mode == "global" else ServiceMode("replicated", replicas=copies)}
    if published:
        kw["endpoint_spec"] = EndpointSpec(ports={published: (target, "tcp")})
    _docker("start that service", cl.services.create, image, **kw)
    _changed()
    U().log("container", f"Added the Swarm service {name} from {image}"
                         + (f", {copies} cop{'y' if copies == 1 else 'ies'}" if copies is not None else ", one on every machine"))
    return {"ok": True}


def _find_free(name):
    sw = swarm()
    if sw["state"] != "manager":
        raise HTTPException(409, sw.get("message") or "Swarm isn't running on this machine. Start one from Clusters first.")
    if any(s["name"].lower() == name.lower() for s in sw["services"]):
        raise HTTPException(409, f"There's already a service called {name}. Choose another name.")


def _service_id(sid):
    if not DOCKER_ID_RE.match(sid or ""):
        raise HTTPException(404, "There's no such service in the Swarm.")
    return sid


@router.post("/api/clusters/swarm/services/{sid}/scale")
def api_scale(sid: str, request: Request, body: dict = Body(...)):
    _service_id(sid)
    n = _copies_in(body.get("copies"))
    svc = _find("services", sid)
    if svc["mode"] == "global":
        raise HTTPException(409, f"{svc['name']} runs one on every machine, so it has no number of copies to change.")
    _gate(request, body)
    cl = _manager()
    _docker("change that service", lambda: cl.services.get(sid).scale(n))
    _changed()
    U().log("container", f"Scaled the Swarm service {svc['name']} from {svc['wanted']} to {n}")
    return {"ok": True}


@router.post("/api/clusters/swarm/services/{sid}/restart")
def api_service_restart(sid: str, request: Request, body: dict = Body(default={})):
    """Starts every copy again, one after another (Docker's rolling update with nothing changed)."""
    _service_id(sid)
    svc = _find("services", sid)
    _gate(request, body)
    cl = _manager()
    _docker("restart that service", lambda: cl.services.get(sid).force_update())
    _changed()
    U().log("container", f"Restarted the Swarm service {svc['name']}")
    return {"ok": True}


@router.post("/api/clusters/swarm/services/{sid}/remove")
def api_service_remove(sid: str, request: Request, body: dict = Body(default={})):
    _service_id(sid)
    svc = _find("services", sid)
    _gate(request, body)
    cl = _manager()
    _docker("remove that service", lambda: cl.services.get(sid).remove())
    _changed()
    U().log("container", f"Removed the Swarm service {svc['name']}")
    return {"ok": True}


@router.post("/api/clusters/swarm/leave")
def api_leave(request: Request, body: dict = Body(default={})):
    """Stop using Swarm on this machine. As a manager that dissolves the cluster's control from here, so it
    needs force; the other machines are left running as they are until you leave Swarm on them too."""
    _gate(request, body)
    cl = _manager()
    _docker("leave the Swarm", lambda: cl.swarm.leave(force=True))
    with _lock:
        d = _load()
        if d.pop("swarm_name", None):
            _save(d)
    _changed()
    U().log("container", "Left the Docker Swarm on this machine")
    return {"ok": True}
