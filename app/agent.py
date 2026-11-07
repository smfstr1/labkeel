"""
LabKeel's agent: runs on another machine and reports it to your main LabKeel (the hub), which shows it on its
Servers page. See docs/servers.md.

It uses the same readings as LabKeel itself (figures, containers, filesystems, pools, drive health) and sends
them every few seconds. It only connects out, so it needs no open port, and it only reads: nothing the hub
sends back can make it change anything.

Settings (agent.compose.yml passes them in):
  LABKEEL_HUB         the hub's address, https://homelab:8787
  LABKEEL_JOIN_CODE   the one-time code from the hub (Servers → Add a server); not needed once joined
  LABKEEL_HUB_KEY     the hub's certificate key, sha256:..., shown beside the code. With it, the agent knows it's
                      talking to your LabKeel even when its certificate is self-signed.
  LABKEEL_DATA_DIR    where it keeps its token (/data)

  python agent.py            run
  python agent.py --health   Docker's health check: did the last report get through?
"""
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


def _connect(hub, pinned):
    """An HTTPS connection to the hub that's been checked: either its certificate is trusted the usual way
    (a Tailscale or Let's Encrypt one, say), or its key is the one this agent was given. Returns
    (connection, the hub's key now). Nothing is sent before the check."""
    u = urlsplit(hub)
    if u.scheme != "https" or not u.hostname:
        raise Untrusted(f"LABKEEL_HUB must be an https:// address, not {hub!r}.")
    port = u.port or 443
    try:
        conn = http.client.HTTPSConnection(u.hostname, port, timeout=TIMEOUT, context=ssl.create_default_context())
        conn.connect()
        return conn, key_of_der(conn.sock.getpeercert(binary_form=True))
    except ssl.SSLCertVerificationError:
        pass
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # checked by hand just below, before anything is sent  # nosec B501
    conn = http.client.HTTPSConnection(u.hostname, port, timeout=TIMEOUT, context=ctx)
    conn.connect()
    key = key_of_der(conn.sock.getpeercert(binary_form=True))
    if not pinned or key != pinned:
        conn.close()
        raise Untrusted("The hub's certificate isn't trusted and its key isn't the one this agent was given "
                        f"(it has {key}). If the hub really did change it, join again with a new code from it.")
    return conn, key


def post(state, path, body, token=None):
    conn, key = _connect(state["hub"], state.get("hub_key"))
    try:
        headers = {"Content-Type": "application/json", "User-Agent": f"LabKeel-agent/{version()}"}
        if token:
            headers["Authorization"] = f"Bearer {state['id']}.{token}"
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
            "error": containers.state.get("error") or stg.get("error")}


def _start_readings():
    import containers
    import main
    import metrics
    for fn in (main.system_sampler, containers.poller, main.smart_poller):
        threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
    for fn in (metrics.gpu_loop, metrics.sample_loop):
        threading.Thread(target=fn, daemon=True, name=fn.__name__).start()


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
            got = post(state, "/api/agent/report", report(), token=state["token"])
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
