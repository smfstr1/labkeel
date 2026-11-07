"""
LabKeel's agent: runs on another machine and reports it to your main LabKeel (the hub), which shows it on its
Servers page, and its files on the Files page. See docs/servers.md.

It uses the same readings as LabKeel itself (figures, containers, filesystems, pools, drive health) and sends
them every few seconds. It only connects out, so it needs no open port.

Files: the hub hands it requests from its Files page (a few are always waiting at the hub, remote.py), and it
runs them with LabKeel's own file code (filesapp.py) against the folders mounted under /browse here, and nothing
else. They can only change anything if LABKEEL_ALLOW_FILE_CHANGES=1 is set here; otherwise they only read.
Containers, settings and everything else on this machine are never changed from the hub.

Settings (agent.compose.yml passes them in):
  LABKEEL_HUB         the hub's address, https://homelab:8787
  LABKEEL_JOIN_CODE   the one-time code from the hub (Add a server); not needed once joined
  LABKEEL_HUB_KEY     the hub's certificate key, sha256:..., shown beside the code. With it, the agent knows it's
                      talking to your LabKeel even when its certificate is self-signed.
  LABKEEL_DATA_DIR    where it keeps its token (/data)
  LABKEEL_ALLOW_FILE_CHANGES   1 = the hub's Files page may change files in the folders under /browse here

  python agent.py            run
  python agent.py --health   Docker's health check: did the last report get through?
"""
import asyncio
import base64
import hashlib
import http.client
import json
import socket
import ssl
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import env

DATA = Path(env.text("LABKEEL_DATA_DIR", "/data"))
STATE = DATA / "agent.json"       # {"hub", "id", "token", "name", "hub_key"}; readable by root only
HEALTH = DATA / "agent-health.json"
HUB = env.text("LABKEEL_HUB").strip().rstrip("/")
CODE = env.text("LABKEEL_JOIN_CODE").strip()
HUB_KEY = env.text("LABKEEL_HUB_KEY").strip()
TIMEOUT = 20


def say(text):
    print(f"[agent] {text}", flush=True)


class Refused(Exception):
    """The hub answered, but no: wrong code, or this agent's token isn't valid there any more."""


class Untrusted(Exception):
    """The hub's certificate isn't the one this agent was given: maybe not your LabKeel."""


# --------------------------------------------------------------------------- talking to the hub
def key_of_der(der):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    cert = x509.load_der_x509_certificate(der)
    spki = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return "sha256:" + base64.urlsafe_b64encode(hashlib.sha256(spki).digest()).decode().rstrip("=")


def _connect(hub, pinned, timeout=TIMEOUT):
    """An HTTPS connection to the hub that's been checked: either its certificate is trusted the usual way
    (a Tailscale or Let's Encrypt one, say), or its key is the one this agent was given. Returns
    (connection, the hub's key now). Nothing is sent before the check."""
    u = urlsplit(hub)
    if u.scheme != "https" or not u.hostname:
        raise Untrusted(f"LABKEEL_HUB must be an https:// address, not {hub!r}.")
    port = u.port or 443
    try:
        conn = http.client.HTTPSConnection(u.hostname, port, timeout=timeout, context=ssl.create_default_context())
        conn.connect()
        return conn, key_of_der(conn.sock.getpeercert(binary_form=True))
    except ssl.SSLCertVerificationError:
        pass
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # checked by hand just below, before anything is sent  # nosec B501
    conn = http.client.HTTPSConnection(u.hostname, port, timeout=timeout, context=ctx)
    conn.connect()
    key = key_of_der(conn.sock.getpeercert(binary_form=True))
    if not pinned or key != pinned:
        conn.close()
        raise Untrusted("The hub's certificate isn't trusted and its key isn't the one this agent was given "
                        f"(it has {key}). If the hub really did change it, join again with a new code from it.")
    return conn, key


def _headers(state, token=None):
    h = {"User-Agent": f"LabKeel-agent/{version()}"}
    if token:
        h["Authorization"] = f"Bearer {state['id']}.{token}"
    return h


def post(state, path, body, token=None, timeout=TIMEOUT):
    conn, key = _connect(state["hub"], state.get("hub_key"), timeout)
    try:
        headers = {"Content-Type": "application/json", **_headers(state, token)}
        conn.request("POST", path, body=json.dumps(body, default=str), headers=headers)
        r = conn.getresponse()
        data = r.read(1_000_000)
    finally:
        conn.close()
    try:
        out = json.loads(data or b"{}")
    except ValueError:
        out = {}
    if r.status in (401, 403):
        raise Refused(out.get("error") or f"refused ({r.status})")
    if r.status >= 400:
        raise RuntimeError(out.get("error") or f"HTTP {r.status}")
    if key != state.get("hub_key"):
        state["hub_key"] = key  # a trusted certificate with a new key (renewed): recognise that from now on
        save(state)
    return out


def load():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save(state):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.chmod(0o600)
    tmp.replace(STATE)


def version():
    try:
        from version import VERSION
        return VERSION
    except Exception:
        return "unknown"


# --------------------------------------------------------------------------- what's reported
def _num(x):
    return round(x, 2) if isinstance(x, float) else x


def report():
    """This machine, as the hub's Servers page shows it."""
    import containers
    import main
    with main._sys_lock:
        cur = dict(main._sys.get("current") or {})
    system = {"cpu": {k: (cur.get("cpu") or {}).get(k) for k in ("percent", "temp", "load")},
              "memory": cur.get("memory"), "uptime": cur.get("uptime"),
              "network": {k: (cur.get("network") or {}).get(k) for k in ("rx_rate", "tx_rate")}}
    try:
        stg = main._storage_raw()
    except Exception as e:
        stg = {"filesystems": [], "pools": [], "error": str(e)}
    fss = [{k: f.get(k) for k in ("source", "type", "mount", "size", "used", "free", "percent")}
           for f in stg.get("filesystems") or [] if not main._fs_hidden_by_default(f)]
    pools = [{k: p.get(k) for k in ("name", "health", "percent", "used", "usable", "errors", "scan")}
             for p in stg.get("pools") or []]
    ctrs = []
    for c in containers.state.get("items") or []:
        st = c.get("stats") or {}
        ctrs.append({"name": c.get("name"), "image": c.get("image"), "state": c.get("state"), "health": c.get("health"),
                     "exit_code": c.get("exit_code"), "uptime": c.get("uptime"), "restarts": c.get("restarts"),
                     "project": c.get("project"), "cpu": st.get("cpu"), "mem": st.get("mem"),
                     "ports": sorted({p.get("host") for p in c.get("ports") or [] if p.get("host")})})
    drives = [{"name": d.get("label") or (d.get("device") or "").replace("/dev/", ""), "model": d.get("model"),
               "status": d.get("status"), "temp": d.get("temp"), "issues": (d.get("issues") or [])[:5]}
              for d in main._drives.get("items") or []]
    try:
        live = main.metrics.live()
    except Exception:
        live = {"power": {}, "gpu": {"items": []}}
    gpus = [{k: g.get(k) for k in ("name", "util", "temp", "mem_used", "mem_total", "power")} for g in live["gpu"]["items"]]
    return {"version": version(), "host": main.HOST_INFO, "system": system,
            "power": {"total": live["power"].get("total")} if live["power"].get("available") else {},
            "containers": ctrs, "filesystems": fss, "pools": pools, "drives": drives, "gpus": gpus,
            "files": files_info(), "error": containers.state.get("error") or stg.get("error")}


def files_info():
    """The folders this machine shares with the Files page (mounted under /browse), and whether it allows changes."""
    import fileops
    import main
    root = main.BROWSE_ROOT
    try:
        real = root.resolve()
        names = sorted(e.name for e in root.iterdir() if e.is_dir() and not main._hidden_part(e.name))[:200]
    except OSError:
        return {"write": fileops.WRITE, "roots": []}
    return {"write": fileops.WRITE, "roots": [{"name": n, "writable": main._writable(real / n, real)} for n in names]}


def new_events(state):
    """File changes made here since the last report, for the hub's activity log too (oldest first)."""
    import db
    try:
        rows = db.events(limit=100, kind="files")
    except Exception:
        return []
    top = max((r["id"] for r in rows), default=0)
    last = state.get("events_sent")
    if last is None or last > top:  # first time (or the database was reset): only what happens from now on
        state["events_sent"] = top
        return []
    return [{"id": r["id"], "text": r["text"], "user": r["user"]} for r in reversed(rows) if r["id"] > last]


def _start_readings():
    import containers
    import fileops
    import main
    import metrics
    for fn in (main.system_sampler, containers.poller, main.smart_poller):
        threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
    for fn in (metrics.gpu_loop, metrics.sample_loop):
        threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
    fileops.start()  # the Bin's clear-out, unfinished uploads, and moves between drives here


# --------------------------------------------------------------------------- file requests from the hub
WORKERS = 6       # requests the hub can have this machine working on at once
WAIT_FOR = 45     # seconds to wait for the hub's answer to a wait (it answers within 25)
_workers = []


def _channel(state):
    """Keep WORKERS threads waiting for file requests from the hub (remote.py)."""
    alive = [t for t in _workers if t.is_alive()]
    _workers[:] = alive
    for _ in range(WORKERS - len(alive)):
        t = threading.Thread(target=_work, args=(state,), daemon=True, name="files")
        t.start()
        _workers.append(t)


def _work(state):
    pause = 1
    while state.get("token"):
        try:
            req = post(state, "/api/agent/wait", {}, token=state["token"], timeout=WAIT_FOR)
            pause = 1
        except Refused:
            return  # removed on the hub: the report loop says so, and starts these again once joined
        except Untrusted:
            time.sleep(300)
            continue
        except Exception:
            time.sleep(pause)
            pause = min(60, pause * 2)
            continue
        if req.get("id"):
            try:
                _serve(state, req)
            except Exception as e:
                say(f"A file request ({req.get('method')} {req.get('path')}) failed: {type(e).__name__}: {e}")


def _fetch_body(state, cid, size):
    conn, _ = _connect(state["hub"], state.get("hub_key"), 120)
    try:
        conn.request("GET", f"/api/agent/body/{cid}", headers=_headers(state, state["token"]))
        r = conn.getresponse()
        data = r.read(size + 1)
        if r.status != 200 or len(data) != size:
            raise RuntimeError(f"couldn't fetch the request's body ({r.status})")
        return data
    finally:
        conn.close()


def _working(state, cid, started):
    if started.is_set():
        return
    try:
        post(state, f"/api/agent/working/{cid}", {}, token=state["token"])
    except Exception:
        pass


def _serve(state, req):
    """Run one request with LabKeel's own file code (filesapp.py) and send the answer back as it comes."""
    cid = str(req["id"])
    body = None
    if isinstance(req.get("body"), str):
        body = base64.b64decode(req["body"])
    elif isinstance(req.get("stream"), int):
        if not 0 <= req["stream"] <= 64 * 1024 * 1024:  # one upload piece at most (fileops.CHUNK_MAX)
            raise ValueError("request body too big")
        body = _fetch_body(state, cid, req["stream"])
    started = threading.Event()
    timer = threading.Timer(5, _working, args=(state, cid, started))  # a slow one: tell the hub it's on its way
    timer.daemon = True
    timer.start()
    try:
        asyncio.run(_answer(state, req, cid, body, started))
    finally:
        timer.cancel()


async def _answer(state, req, cid, body, started):
    import filesapp
    fw = req.get("forwarded") if isinstance(req.get("forwarded"), dict) else {}
    fw = {"user": fw.get("user"), "via": str(fw.get("via") or "the hub")[:64], "session": str(fw.get("session") or "")[:64]}
    status, hdrs, chunks = await filesapp.call(str(req.get("method")), str(req.get("path")), str(req.get("query") or ""),
                                               req.get("headers") if isinstance(req.get("headers"), dict) else {}, body,
                                               forwarded=fw)
    started.set()
    conn = None
    try:
        conn = await asyncio.to_thread(_reply_open, state, cid, status, hdrs)
        async for c in chunks:
            await asyncio.to_thread(conn.send, b"%x\r\n" % len(c) + c + b"\r\n")
        await asyncio.to_thread(conn.send, b"0\r\n\r\n")
        r = await asyncio.to_thread(conn.getresponse)
        r.read(10000)
    finally:
        await chunks.aclose()
        if conn:
            conn.close()


def _reply_open(state, cid, status, hdrs):
    conn, _ = _connect(state["hub"], state.get("hub_key"), 130)  # checked before anything is sent, as ever
    conn.putrequest("POST", f"/api/agent/reply/{cid}", skip_accept_encoding=True)
    for k, v in {**_headers(state, state["token"]), "Content-Type": "application/octet-stream",
                 "Transfer-Encoding": "chunked", "X-LabKeel-Status": str(int(status)),
                 "X-LabKeel-Headers": json.dumps(hdrs)}.items():
        conn.putheader(k, v)
    conn.endheaders()
    return conn


def _healthy(ok, note=""):
    try:
        HEALTH.write_text(json.dumps({"t": time.time(), "ok": ok, "note": note}))
    except OSError:
        pass


# --------------------------------------------------------------------------- run
def _code_id(code):
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()[:16]


def join(state):
    if not (HUB and CODE):
        raise Refused("Not joined yet: start the agent with LABKEEL_HUB and LABKEEL_JOIN_CODE from your LabKeel "
                      "(the arrow beside the machine's name, then Add a server).")
    state.update(hub=HUB, hub_key=HUB_KEY or None)
    got = post(state, "/api/agent/join", {"code": CODE, "report": report()})
    state.update(id=got["id"], token=got["token"], name=got.get("name"), joined_with=_code_id(CODE))
    save(state)
    say(f"Joined {HUB} as {state['name']}.")
    return got.get("interval") or 10


def run():
    _healthy(True, "starting")
    _start_readings()
    time.sleep(3)  # the first readings
    state = load()
    if CODE and state.get("joined_with") != _code_id(CODE):
        state = {}  # a new code (a different LabKeel, or joining again after being removed): join with it
    interval, failures = 10, 0
    while True:
        try:
            if not state.get("token"):
                interval = join(state)
            rep = report()
            rep["events"] = new_events(state)
            got = post(state, "/api/agent/report", rep, token=state["token"])
            if rep["events"]:
                state["events_sent"] = rep["events"][-1]["id"]
                save(state)
            _channel(state)
            interval = max(5, min(300, int(got.get("interval") or interval)))
            if failures:
                say(f"Reporting to {state['hub']} again.")
            failures = 0
            _healthy(True)
        except Refused as e:
            say(f"The hub said no: {e}")
            _healthy(False, str(e))
            if not state.get("token"):
                # a join code only works once: trying it again won't help, so wait to be given a new one
                say("Waiting for a new join code: start the agent again with the command from your LabKeel.")
                while True:
                    time.sleep(3600)
            time.sleep(300)  # its token was refused (removed on the hub?): keep asking now and then
            continue
        except Untrusted as e:
            say(str(e))
            _healthy(False, str(e))
            time.sleep(300)
            continue
        except (OSError, http.client.HTTPException, RuntimeError, socket.timeout) as e:
            failures += 1
            if failures in (1, 10) or failures % 60 == 0:
                say(f"Couldn't reach the hub ({type(e).__name__}: {e}). Trying again every {interval} s.")
            _healthy(False, str(e))
        time.sleep(interval)


def health():
    """Exit 0 if a report got through in the last 2 minutes (or the agent only just started)."""
    try:
        h = json.loads(HEALTH.read_text())
    except (OSError, ValueError):
        return 1
    return 0 if h.get("ok") and time.time() - h["t"] < 120 else 1


if __name__ == "__main__":
    if "--health" in sys.argv:
        sys.exit(health())
    say(f"LabKeel agent {version()} starting")
    run()
