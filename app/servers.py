"""
Several servers: other machines run LabKeel's agent (agent.py) and report in to this LabKeel, the hub.

How a machine joins:
  1. An admin asks for a join code here (Servers → Add a server). It works once, for 15 minutes.
  2. The agent is started on the other machine with this LabKeel's address, the code, and this LabKeel's
     certificate key (so it can tell it's really talking to this LabKeel, even with a self-signed certificate).
  3. It sends the code to POST /api/agent/join and gets its own secret token back. From then on it sends a
     report every few seconds to POST /api/agent/report: figures, containers, filesystems, pools and drives.

The agent connects out to the hub, so the other machine needs no port opened. Beyond its reports, the hub can
only ask it for files (remote.py): the Files page's own requests, in the folders shared under /browse there, and
changes only if LABKEEL_ALLOW_FILE_CHANGES=1 is set there. Its containers and settings are shown, not changed.
A machine is removed by deleting its token, after which its reports and file requests are refused.

Stored in data/servers.json: each machine's name, a hash of its token (never the token), when it joined and was
last heard from, and its latest report (so an offline machine still shows what it looked like).
"""
import hashlib
import hmac
import re
import secrets
import sys
import threading
import time

from fastapi import APIRouter, Body, HTTPException, Request

import containers

router = APIRouter()

INTERVAL = 10           # seconds between an agent's reports (it's told this with every answer)
OFFLINE_AFTER = 60      # no report for this long: shown as offline
ALERT_AFTER = 5 * 60    # ...and after this long, an alert (if alerts for servers are on)
CODE_FOR = 15 * 60      # a join code works for this long, once
CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L: easy to read out and type
MAX_SERVERS = 50
REPO = "https://github.com/smfstr1/labkeel.git"

_lock = threading.Lock()
_reports = {}           # server id -> {"t": when received, "data": the report}
_saved = {}             # server id -> when its latest report was last written to disk


def M():
    return sys.modules["main"]


def U():
    return sys.modules["upkeep"]


def S():
    return sys.modules["security"]


def _load():
    d = U()._read("servers.json", {})
    d.setdefault("servers", {})
    d.setdefault("codes", {})
    return d


def _save(d):
    U()._write("servers.json", d)


def _hash(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def hub_key():
    """The public key of the certificate this LabKeel serves, as agents pin it: 'sha256:<base64url>'.
    None without HTTPS (behind your own proxy, agents check its certificate the usual way)."""
    tls = sys.modules.get("tls")
    path = (tls.state.get("cert") if tls else None) or ""
    if not path:
        return None
    try:
        from cryptography import x509
        with open(path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        return key_of(cert)
    except Exception:
        return None


def key_of(cert):
    """sha256 of a certificate's public key (SubjectPublicKeyInfo): stays the same when the certificate is
    renewed with the same key."""
    import base64

    from cryptography.hazmat.primitives import serialization
    spki = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return "sha256:" + base64.urlsafe_b64encode(hashlib.sha256(spki).digest()).decode().rstrip("=")


# --------------------------------------------------------------------------- summaries
def _num(x, lo=0.0, hi=1e18):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None


def summarise(rep):
    """The few figures a server card shows, from a report (an agent's, or this machine's own)."""
    rep = rep or {}
    sysd, cs = rep.get("system") or {}, rep.get("containers") or []
    fss = [f for f in rep.get("filesystems") or [] if isinstance(f, dict)]
    root = next((f for f in fss if f.get("mount") == "/"), None) or (max(fss, key=lambda f: f.get("size") or 0) if fss else None)
    pools = [p for p in rep.get("pools") or [] if isinstance(p, dict)]
    # the machine's own drives: not ZFS datasets (their pools count instead), nor network shares, which could
    # be another server's storage counted twice
    local = [f for f in fss if f.get("type") != "zfs" and not re.match(r"^(fuse|nfs|cifs|smb|9p|sshfs)", f.get("type") or "")]
    host = rep.get("host") or {}
    return {
        "hostname": host.get("hostname"), "os": host.get("os"), "cpu_model": host.get("cpu_model"),
        "uptime": _num(sysd.get("uptime")),
        "cpu": _num((sysd.get("cpu") or {}).get("percent"), 0, 100),
        "memory": _num((sysd.get("memory") or {}).get("percent"), 0, 100),
        "disk": _num(root.get("percent"), 0, 100) if root else None, "disk_mount": root.get("mount") if root else None,
        "storage_used": sum(f.get("used") or 0 for f in local) + sum(p.get("used") or 0 for p in pools),
        "storage_size": sum(f.get("size") or 0 for f in local) + sum(p.get("usable") or 0 for p in pools),
        "total": len(cs), "running": sum(1 for c in cs if c.get("state") == "running"),
        "power": _num((rep.get("power") or {}).get("total"), 0, 1e5),
        "pools": [{"name": p.get("name"), "health": p.get("health"), "percent": _num(p.get("percent"), 0, 100)} for p in pools],
    }


def own_report():
    """This machine, in the same shape as an agent's report."""
    m = M()
    with m._sys_lock:
        cur = dict(m._sys.get("current") or {})
    try:
        stg = m._storage_raw()
    except Exception:
        stg = {"filesystems": [], "pools": []}
    try:
        power = m.metrics.live()["power"]
    except Exception:
        power = {}
    return {"host": m.HOST_INFO, "system": cur, "power": power, "containers": containers.state.get("items") or [],
            "filesystems": [f for f in stg.get("filesystems") or [] if not m._fs_hidden_by_default(f)],
            "pools": stg.get("pools") or []}


def _state(srv, t):
    if not t:
        return "waiting"
    return "online" if time.time() - t < OFFLINE_AFTER else "offline"


def listing(with_containers=False):
    d = _load()
    now = time.time()
    out = []
    for sid, s in d["servers"].items():
        r = _reports.get(sid) or ({"t": s.get("last_seen"), "data": s.get("last")} if s.get("last") else None)
        t = (r or {}).get("t") or s.get("last_seen")
        out.append({"id": sid, "name": s.get("name"), "joined": s.get("joined"), "last_seen": t,
                    "state": _state(s, t), "agent_version": s.get("agent_version"), "ip": s.get("ip"),
                    "problems": len(conditions_for(sid, s, r, now)), **summarise((r or {}).get("data"))})
        if with_containers:
            out[-1]["container_list"] = [{k: c.get(k) for k in ("name", "image", "state", "health", "exit_code", "uptime", "cpu", "mem")}
                                     for c in ((r or {}).get("data") or {}).get("containers") or []]
    out.sort(key=lambda x: (x["name"] or "").lower())
    pending = [{"name": c["name"], "expires": c["expires"]} for c in d["codes"].values() if c["expires"] > now]
    return out, pending


# --------------------------------------------------------------------------- alerts (upkeep's alert loop asks)
def conditions_for(sid, s, r, now, a=None):
    """Problems on one server, as {key: (level, title, detail)}."""
    a = a or {"disk_threshold": 90, "pool_threshold": 80}
    out = {}
    name = s.get("name") or sid
    t = (r or {}).get("t") or s.get("last_seen")
    if not t or now - t >= ALERT_AFTER:
        if t:
            out[f"server:{sid}"] = ("bad", f"{name} isn't reporting",
                                    f"Last heard from {time.strftime('%H:%M on %d %b', time.localtime(t))}. "
                                    "Check the machine is on and its LabKeel agent is running.")
        return out
    rep = (r or {}).get("data") or {}
    for c in rep.get("containers") or []:
        crashed = c.get("state") == "exited" and c.get("exit_code") not in (0, 143, None)
        if crashed or c.get("state") == "restarting" or c.get("health") == "unhealthy":
            out[f"server:{sid}:container:{c.get('name')}"] = ("bad", f"{c.get('name')} on {name} has stopped or keeps restarting",
                                                               "Open Servers in LabKeel for details.")
    for f in rep.get("filesystems") or []:
        if f.get("type") != "zfs" and (_num(f.get("percent")) or 0) >= a["disk_threshold"]:
            out[f"server:{sid}:disk:{f.get('mount')}"] = ("warn", f"{f.get('mount')} on {name} is {round(f['percent'])}% full", "")
    for p in rep.get("pools") or []:
        if p.get("health") and p["health"] != "ONLINE":
            out[f"server:{sid}:pool:{p.get('name')}"] = ("bad", f"ZFS pool {p.get('name')} on {name} is {p['health']}", "")
        elif (_num(p.get("percent")) or 0) >= a["pool_threshold"]:
            out[f"server:{sid}:poolspace:{p.get('name')}"] = ("warn", f"ZFS pool {p.get('name')} on {name} is {round(p['percent'])}% full", "")
    for dr in rep.get("drives") or []:
        if dr.get("status") in ("warn", "bad"):
            out[f"server:{sid}:drive:{dr.get('name')}"] = (dr["status"], f"Drive {dr.get('name')} on {name} needs attention",
                                                           "; ".join(dr.get("issues") or [])[:300])
    return out


def conditions(a):
    d = _load()
    now = time.time()
    out = {}
    for sid, s in d["servers"].items():
        r = _reports.get(sid) or ({"t": s.get("last_seen"), "data": s.get("last")} if s.get("last") else None)
        out.update(conditions_for(sid, s, r, now, a))
    return out


# --------------------------------------------------------------------------- endpoints for the page
@router.get("/api/servers")
def api_servers(containers: bool = False):
    """Every machine: this one first, then each agent, with the figures its card shows (and, with
    ?containers=true, each one's containers, for the Containers page's "All servers")."""
    items, pending = listing(containers)
    own = summarise(own_report())
    return {"this": {"id": "this", "name": M().HOST_INFO.get("hostname") or "This server", "state": "online", **own},
            "servers": items, "pending": pending, "interval": INTERVAL}


@router.get("/api/servers/{sid}")
def api_server(sid: str, request: Request):
    """One machine's latest report, for its page. Its shared folders only for admins (viewers don't see Files)."""
    d = _load()
    s = d["servers"].get(sid)
    if not s:
        raise HTTPException(404, "There's no server with that name here.")
    r = _reports.get(sid) or ({"t": s.get("last_seen"), "data": s.get("last")} if s.get("last") else {})
    t = r.get("t")
    rep = dict(r.get("data") or {})
    if S().role(request) != "admin":
        rep.pop("files", None)
    else:
        rep["files_connected"] = sys.modules["remote"].connected(sid) if "remote" in sys.modules else False
    return {"id": sid, "name": s.get("name"), "joined": s.get("joined"), "last_seen": t, "state": _state(s, t),
            "agent_version": s.get("agent_version"), "ip": s.get("ip"), "report": rep,
            "problems": [{"level": v[0], "title": v[1], "detail": v[2]} for v in conditions_for(sid, s, r, time.time()).values()]}


NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")


def _name(v):
    v = str(v or "").strip()
    if not NAME_RE.match(v):
        raise HTTPException(400, "Give the server a name: letters, numbers, spaces, dots and dashes, up to 40.")
    return v


HUB_RE = re.compile(r"^https://(\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.-]{1,253})(:\d{1,5})?$")


def hub_url(request, given=None):
    """This LabKeel's address for the agent: as typed in the dialog (how the other machine reaches it), or the
    one the browser used."""
    if given:
        given = str(given).strip().rstrip("/")
        if not HUB_RE.match(given):
            raise HTTPException(400, "This LabKeel's address should look like https://homelab:8787.")
        return given
    host = request.headers.get("host") or ""
    return f"https://{host}" if host else ""


def command(hub, code, key):
    """What to run on the other machine."""
    envs = f"LABKEEL_HUB={hub} LABKEEL_JOIN_CODE={code}" + (f" LABKEEL_HUB_KEY={key}" if key else "")
    # safe to run again on a machine that has it already (to join afresh with a new code, say)
    # and agent.local.yml, if it's there: the folders this machine shares with Files (git leaves that file alone)
    return (f"[ -d labkeel-agent ] || git clone {REPO} labkeel-agent\n"
            f"cd labkeel-agent && {envs} \\\n"
            f"  docker compose -f agent.compose.yml $([ -f agent.local.yml ] && echo -f agent.local.yml) up -d --build")


@router.post("/api/servers/code")
def api_code(request: Request, body: dict = Body(...)):
    """A one-time join code for a new server (admins, the password again)."""
    name = _name(body.get("name"))
    hub = hub_url(request, body.get("hub"))
    S().confirm_password(request, body.get("password"))
    now = time.time()
    with _lock:
        d = _load()
        d["codes"] = {h: c for h, c in d["codes"].items() if c["expires"] > now and c["name"] != name}
        if len(d["servers"]) >= MAX_SERVERS:
            raise HTTPException(409, f"LabKeel looks after up to {MAX_SERVERS} servers.")
        if any(s.get("name", "").lower() == name.lower() for s in d["servers"].values()):
            raise HTTPException(409, f"There's already a server called {name}.")
        if len(d["codes"]) >= 5:
            raise HTTPException(409, "There are already 5 join codes waiting. Cancel one, or wait for them to run out.")
        raw = "".join(secrets.choice(CODE_CHARS) for _ in range(9))
        code = f"{raw[:3]}-{raw[3:6]}-{raw[6:]}"
        d["codes"][_hash(raw)] = {"name": name, "expires": now + CODE_FOR, "made": now,
                                  "by": (S().ACTOR.get() or {}).get("user")}
        _save(d)
    U().log("settings", f"Made a join code for a new server, {name}")
    key = hub_key()
    return {"name": name, "code": code, "expires": now + CODE_FOR, "hub": hub, "key": key, "command": command(hub, code, key)}


@router.post("/api/servers/code/cancel")
def api_code_cancel(body: dict = Body(...)):
    name = str(body.get("name") or "")
    with _lock:
        d = _load()
        d["codes"] = {h: c for h, c in d["codes"].items() if c["name"] != name}
        _save(d)
    return {"ok": True}


@router.post("/api/servers/{sid}/rename")
def api_rename(sid: str, body: dict = Body(...)):
    name = _name(body.get("name"))
    with _lock:
        d = _load()
        if sid not in d["servers"]:
            raise HTTPException(404, "There's no server with that name here.")
        old = d["servers"][sid].get("name")
        d["servers"][sid]["name"] = name
        _save(d)
    U().log("settings", f"Renamed the server {old} to {name}")
    return {"ok": True}


@router.post("/api/servers/{sid}/remove")
def api_remove(sid: str, request: Request, body: dict = Body(default={})):
    """Forget a server: its token stops working, so its agent's reports are refused (admins, password again)."""
    S().confirm_password(request, body.get("password"))
    with _lock:
        d = _load()
        s = d["servers"].pop(sid, None)
        if not s:
            raise HTTPException(404, "There's no server with that name here.")
        _save(d)
        _reports.pop(sid, None)
    U().log("settings", f"Removed the server {s.get('name')}. Its agent can be stopped and removed on that machine.")
    return {"ok": True}


RELAY_PER_HOUR = 300
_relayed = {}  # server id -> [hour, lines repeated in it, said so when it ran over]


def _repeat_events(sid, name, events):
    """File changes made on another machine (through Files here, or there by a copy) also go in this machine's
    activity log, under whoever made them. At most RELAY_PER_HOUR an hour from each machine, so one that
    misbehaves can't push everything else out of the log."""
    actor = S().ACTOR
    hour = int(time.time() // 3600)
    seen = _relayed.get(sid)
    if not seen or seen[0] != hour:
        seen = _relayed[sid] = [hour, 0, False]  # hour, lines repeated, said so when it ran over
    keep = events[:max(0, RELAY_PER_HOUR - seen[1])]
    seen[1] += len(keep)
    if len(keep) < len(events) and not seen[2]:
        seen[2] = True
        U().log("files", f"On {name}: more file changes this hour than are repeated here. Its own activity log has them all.")
    events = keep
    for e in events:
        token = actor.set({"user": e["user"], "ip": name})
        try:
            U().log("files", f"On {name}: {e['text']}")
        finally:
            actor.reset(token)


# --------------------------------------------------------------------------- endpoints for agents (no session:
# they're public in security.PUBLIC and check their own code or token)
def _bearer(request):
    h = request.headers.get("authorization") or ""
    m = re.fullmatch(r"Bearer ([0-9a-f]{12})\.([A-Za-z0-9_-]{20,100})", h.strip())
    return (m.group(1), m.group(2)) if m else (None, None)


def agent_auth(request):
    """The agent sending this request, by its token: (server id, its record), or 401."""
    sid, token = _bearer(request)
    s = _load()["servers"].get(sid) if sid else None
    if not s or not hmac.compare_digest(s.get("token") or "", _hash(token or "")):
        raise HTTPException(401, "This agent isn't joined to this LabKeel (it may have been removed). Join it again with a new code.")
    return sid, s


def _text(x, n):
    return x[:n] if isinstance(x, str) else None


def _clean_files(f):
    """What a machine shares with Files: whether it allows changes, and its top folders."""
    if not isinstance(f, dict):
        return None
    roots = [{"name": _text(r.get("name"), 255), "writable": r.get("writable") is True}
             for r in (f.get("roots") if isinstance(f.get("roots"), list) else [])[:200]
             if isinstance(r, dict) and _text(r.get("name"), 255)]
    return {"write": f.get("write") is True, "roots": [r for r in roots if "/" not in r["name"]]}


def _clean_events(ev):
    """Lines from the machine's own activity log (its file changes), to repeat in this one's."""
    out = []
    for e in (ev if isinstance(ev, list) else [])[:100]:
        if isinstance(e, dict) and isinstance(e.get("id"), int) and _text(e.get("text"), 500):
            out.append({"id": e["id"], "text": " ".join(e["text"][:500].split()), "user": _text(e.get("user"), 64)})
    return out


def _clean_report(body):
    """Keeps a report to what's expected, so a broken or hostile agent can't fill the hub with anything else."""
    if not isinstance(body, dict):
        raise HTTPException(400, "A report is a JSON object.")
    keep = {}
    for k in ("host", "system", "power"):
        if isinstance(body.get(k), dict):
            keep[k] = body[k]
    for k, n in (("containers", 500), ("filesystems", 200), ("pools", 50), ("drives", 100), ("gpus", 16)):
        v = body.get(k)
        if isinstance(v, list):
            keep[k] = [x for x in v[:n] if isinstance(x, dict)]
    for k in ("version", "error"):
        if isinstance(body.get(k), str):
            keep[k] = body[k][:300]
    files = _clean_files(body.get("files"))
    if files is not None:
        keep["files"] = files
    return keep


@router.post("/api/agent/join")
async def api_agent_join(request: Request):
    """An agent trades a join code for its own token. Wrong codes count towards the same pause as wrong passwords."""
    body, err = await S()._read_json(request, cap=1_000_000)
    if err:
        return err
    # wrong codes are counted apart from people's wrong passwords: a stuck agent mustn't pause anyone's sign-in
    ip = S().client_ip(request)
    who = f"agent|{ip}"
    wait, entry = S()._begin_attempt(who)
    if wait:
        raise HTTPException(429, S()._wait_msg(wait))
    raw = re.sub(r"[^A-Z0-9]", "", str(body.get("code") or "").upper())
    now = time.time()
    with _lock:
        d = _load()
        c = d["codes"].get(_hash(raw)) if len(raw) == 9 else None
        if not c or c["expires"] <= now:
            S()._failed(who, entry, "agent")
            raise HTTPException(403, "That join code isn't right, or has run out. Make a new one in LabKeel: the arrow beside the machine's name, then Add a server.")
        d["codes"].pop(_hash(raw))
        sid, token = secrets.token_hex(6), secrets.token_urlsafe(32)
        rep = _clean_report(body.get("report") or {})
        d["servers"][sid] = {"name": c["name"], "token": _hash(token), "joined": now, "by": c.get("by"), "ip": ip,
                             "last_seen": now, "agent_version": rep.get("version"), "last": rep}
        _save(d)
        _reports[sid] = {"t": now, "data": rep}
    S()._succeeded(who, entry, "agent")
    U().log("settings", f"{c['name']} joined as a server (from {ip})")
    return {"id": sid, "token": token, "name": c["name"], "interval": INTERVAL}


@router.post("/api/agent/report")
async def api_agent_report(request: Request):
    sid, s = agent_auth(request)
    body, err = await S()._read_json(request, cap=1_000_000)
    if err:
        return err
    rep = _clean_report(body)
    _repeat_events(sid, s.get("name") or sid, _clean_events(body.get("events")))
    now = time.time()
    _reports[sid] = {"t": now, "data": rep}
    if now - _saved.get(sid, 0) > 300 or s.get("agent_version") != rep.get("version"):
        # on disk every 5 minutes is plenty: what an offline machine last looked like, and when
        with _lock:
            d = _load()
            if sid in d["servers"]:
                d["servers"][sid].update(last_seen=now, last=rep, agent_version=rep.get("version"), ip=S().client_ip(request))
                _save(d)
        _saved[sid] = now
    return {"ok": True, "interval": INTERVAL, "name": s.get("name")}

