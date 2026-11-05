"""
Updating a container to its newest image, from the Containers page, with an automatic way back.

For a container started by Docker Compose, LabKeel does what you'd do by hand in its project folder, on the
host (so the project's own compose files, .env and settings are used exactly as they are):

  1. docker compose pull <service>              download the newer image
  2. docker compose up -d --no-deps <service>   re-create the container on it (nothing else in the project)
  3. watch the new container for a short while: it must keep running (and pass its health check, if it has one)
  4. if it doesn't, put the previous image back under the same name and re-create the container on it again

Admins only, password again, and the activity log says who updated what and how it went. Containers not
started by Compose are left alone: LabKeel can't know every setting they were made with.
"""
import contextvars
import re
import sys
import threading
import time

from fastapi import APIRouter, Body, HTTPException, Request

import containers
import selfupdate

router = APIRouter()

PULL_TIMEOUT = 900     # seconds a pull may take (big images, slow lines)
UP_TIMEOUT = 300
WATCH = 20             # seconds the new container must keep running (without a health check)
HEALTH_WAIT = 180      # the most we wait for a health check to pass
_jobs = {}             # container name -> the latest update job (what the page shows)
_lock = threading.Lock()


def U():
    return sys.modules["upkeep"]


class Refused(Exception):
    """Can't update this container; the message says why."""


def compose_info(c):
    """The compose project a container belongs to, as selfupdate.compose_args wants it. Refused if none."""
    labels = (c.attrs.get("Config") or {}).get("Labels") or {}
    info = {"project": labels.get("com.docker.compose.project"), "service": labels.get("com.docker.compose.service"),
            "workdir": labels.get("com.docker.compose.project.working_dir"),
            "files": [f for f in (labels.get("com.docker.compose.project.config_files") or "").split(",") if f]}
    if not all(info.values()):
        raise Refused("This container wasn't started by Docker Compose, so LabKeel can't re-create it with the same "
                      "settings. Pull the new image and re-create it the way you made it.")
    if not (selfupdate._SAFE_NAME.match(info["project"]) and selfupdate._SAFE_NAME.match(info["service"])
            and selfupdate._SAFE_PATH.match(info["workdir"]) and all(selfupdate._SAFE_PATH.match(f) for f in info["files"])):
        raise Refused("This container's compose project has a name or folder LabKeel won't pass to a command.")
    if not sys.modules["main"].HOST_NS:
        raise Refused("LabKeel can't run commands on this machine (DASH_HOST_NS is off), so it can't run docker compose.")
    return info


def _split_ref(ref):
    """'lscr.io/linuxserver/sonarr:latest' -> ('lscr.io/linuxserver/sonarr', 'latest')."""
    ref = ref.split("@", 1)[0]
    if ":" in ref.rsplit("/", 1)[-1]:
        repo, tag = ref.rsplit(":", 1)
        return repo, tag
    return ref, "latest"


def _find_service(cl, info):
    """The service's container now (Compose gives the re-created one a new ID)."""
    found = cl.containers.list(all=True, filters={"label": [f"com.docker.compose.project={info['project']}",
                                                           f"com.docker.compose.service={info['service']}"]})
    return found[0] if found else None


def _healthy_enough(cl, info, log):
    """Wait for the re-created container to prove itself. Returns None if fine, else why not."""
    end = time.time() + HEALTH_WAIT
    started = time.time()
    first_restarts = None
    while time.time() < end:
        c = _find_service(cl, info)
        if c is None:
            return "the new container disappeared"
        st = c.attrs.get("State") or {}
        health = (st.get("Health") or {}).get("Status")
        restarts = c.attrs.get("RestartCount", 0)
        first_restarts = restarts if first_restarts is None else first_restarts
        if st.get("Status") in ("exited", "dead"):
            return f"the new container stopped (exit code {st.get('ExitCode')})"
        if restarts > first_restarts or st.get("Restarting"):
            return "the new container keeps restarting"
        if health == "unhealthy":
            return "the new container failed its health check"
        if health == "healthy":
            log("Health check passed")
            return None
        if health is None and time.time() - started >= WATCH:
            log(f"Still running after {WATCH} seconds")
            return None
        time.sleep(2)
        c.reload()
    return "the new container didn't pass its health check in time"


def run(name, info, ref, old_image):
    """The update itself (in a thread). Every step is logged to the job, which the page polls."""
    job = _jobs[name]
    say = lambda text: job["steps"].append({"t": time.time(), "text": text})  # noqa: E731
    host, compose = selfupdate.host, selfupdate.compose_args(info)
    cl = containers.dclient()
    try:
        say(f"Downloading the newest {ref}")
        r = host(compose + ["pull", info["service"]], PULL_TIMEOUT)
        if r.returncode != 0:
            raise RuntimeError(f"the download failed: {selfupdate._err(r)[0]}")
        try:
            new_image = cl.images.get(ref).id
        except Exception:
            new_image = None
        if new_image == old_image:
            job.update(state="done", result="current")
            say("Already on the newest image: nothing to change")
            return
        say("Re-creating the container on the new image")
        r = host(compose + ["up", "-d", "--no-deps", info["service"]], UP_TIMEOUT)
        if r.returncode != 0:
            raise RuntimeError(f"docker compose couldn't start it: {selfupdate._err(r)[0]}")
        containers.refresh_soon()
        say("Checking the new container works")
        problem = _healthy_enough(cl, info, say)
        if problem is None:
            job.update(state="done", result="updated")
            say("Updated")
            U().log("container", f"Updated {name} to the newest {ref}")
            return
        say(f"Problem: {problem}. Going back to the previous image")
        repo, tag = _split_ref(ref)
        cl.api.tag(old_image, repo, tag, force=True)
        r = host(compose + ["up", "-d", "--no-deps", info["service"]], UP_TIMEOUT)
        if r.returncode != 0:
            raise RuntimeError(f"{problem}, and going back failed too: {selfupdate._err(r)[0]}")
        job.update(state="failed", result="rolled_back", detail=problem)
        say("Back on the previous image")
        U().log("error", f"Updating {name} failed ({problem}), so it was put back on its previous image")
    except Exception as e:
        job.update(state="failed", result="error", detail=str(e))
        say(f"Stopped: {e}")
        U().log("error", f"Updating {name} failed: {e}")
    finally:
        job["finished"] = time.time()
        containers.refresh_soon()


@router.post("/api/containers/{cid}/update")
def api_update(cid: str, request: Request, body: dict = Body(default={})):
    """Update one container to its newest image (admins, password again). Runs in the background: the page
    follows it with GET /api/containers/{cid}/update."""
    c = containers._get(cid)
    if c.id == containers.SELF_ID or (containers.SELF_ID and c.id.startswith(containers.SELF_ID)):
        raise HTTPException(409, "LabKeel updates itself from Settings → LabKeel.")
    try:
        info = compose_info(c)
    except Refused as e:
        raise HTTPException(409, str(e)) from e
    ref = (c.attrs.get("Config") or {}).get("Image") or ""
    if not ref or "@" in ref or re.match(r"^sha256:", ref):
        raise HTTPException(409, "This container is fixed to one exact image, so there's nothing newer to update to.")
    sec = sys.modules["security"]
    sec.confirm_password(request, body.get("password"))
    with _lock:
        cur = _jobs.get(c.name)
        if cur and cur["state"] == "running":
            raise HTTPException(409, "It's already being updated.")
        _jobs[c.name] = {"state": "running", "started": time.time(), "by": (sec.ACTOR.get() or {}).get("user"),
                         "image": ref, "steps": [], "result": None, "detail": None, "finished": None}
    U().log("container", f"Updating {c.name} to the newest {ref}")
    ctx = contextvars.copy_context()  # so the log lines say who asked
    threading.Thread(target=ctx.run, args=(run, c.name, info, ref, c.attrs.get("Image")),
                     daemon=True, name=f"update-{c.name}").start()
    return {"ok": True}


@router.get("/api/containers/{cid}/update")
def api_update_status(cid: str):
    """How the latest update of this container is going (or went). Asked by name: the re-created
    container has a new ID."""
    return _jobs.get(cid) or _jobs.get(containers._get(cid).name) or {"state": None}
