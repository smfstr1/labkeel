"""
Updating LabKeel from its own Settings page.

LabKeel can't replace its own container from inside it: the moment Docker stops the old container, everything
running in it stops too, including whatever was doing the update. So the update runs on the host, as a
short-lived systemd unit (systemd-run), and does exactly what you'd type yourself in LabKeel's folder:

  installed from source (build: .)   git pull --ff-only, then docker compose up -d --build <service>
  installed from an image            docker compose pull <service>, then docker compose up -d <service>

Everything about the install is read from LabKeel's own container (Docker Compose labels it with its project,
folder, compose files and service) and from `docker compose config`, so nothing needs setting up. Git runs as
the person who owns the folder, with their own git sign-in, so private repositories work too.

The update writes its progress to data/update.log and ends with a line "LABKEEL-UPDATE-EXIT <code>". The old
LabKeel watches that file while it's still running (a failed build leaves it running), and the new one reads
it on start-up, so the activity log always says how it went, and who asked.

What it needs: the compose set-up from INSTALL.md (privileged, pid: host, so LABKEEL_HOST_COMMANDS is on), a host that
runs systemd, and git on the host for source installs. Without them, Settings shows the commands instead.
"""
import asyncio
import contextvars
import json
import re
import shlex
import sys
import threading
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Body, HTTPException, Request

import containers
import updates
import version
from version import USER_AGENT, VERSION

router = APIRouter()

EXIT_MARK = "LABKEEL-UPDATE-EXIT"
HOST_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"
INFO_TTL = 300             # how long what we learnt about the install is trusted (seconds)
FETCH_TIMEOUT = 90         # git fetch
GIVE_UP_AFTER = 3600       # an update that hasn't finished after an hour is reported as lost
MAX_CHANGES = 30           # commit subjects shown

_lock = threading.Lock()
_state = {"info": None, "info_t": 0.0, "watching": False}


def M():
    return sys.modules["main"]


def U():
    return sys.modules["upkeep"]


class NotPossible(Exception):
    """This install can't update itself; the message says why, in words for the page."""


# --------------------------------------------------------------------------- files in the data folder
def _data(name):
    return Path(M().DATA_DIR) / name


def _load():
    """data/update.json: the latest check, the update in progress (if any) and how the last one went."""
    try:
        d = json.loads(_data("update.json").read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d):
    p = _data("update.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"update.json.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(d, indent=2))
    tmp.replace(p)


def log_tail(lines=60):
    try:
        text = _data("update.log").read_text(errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _exit_code(text):
    m = re.search(rf"^{EXIT_MARK} (\d+)\s*$", text, re.M)
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- running things on the host
def host(args, timeout=30, input=None):
    """Run a command on the host (in its mount namespace) with a plain, predictable environment."""
    env = ["env", "-i", f"PATH={HOST_PATH}", "LANG=C.UTF-8", "GIT_TERMINAL_PROMPT=0", *args]
    return M().host_cmd(env, timeout, input) if input is not None else M().host_cmd(env, timeout)


def _ok(r):
    return r.returncode == 0


def _out(r):
    return (r.stdout or "").strip()


def _err(r):
    return ((r.stderr or "") or (r.stdout or "")).strip().splitlines()[-1:] or [f"exit code {r.returncode}"]


def as_owner(info, args):
    """Run git as whoever owns the folder, so their own git sign-in (SSH key, gh, credential helper) is used."""
    owner = info.get("owner")
    if owner and owner != "root":
        return ["runuser", "-u", owner, "--", "env", "GIT_TERMINAL_PROMPT=0", *args]
    return list(args)


def compose_args(info):
    """docker compose, pointed at LabKeel's own project exactly as it was started."""
    out = ["docker", "compose", "-p", info["project"], "--project-directory", info["workdir"]]
    for f in info["files"]:
        out += ["-f", f]
    return out


# --------------------------------------------------------------------------- what kind of install is this?
_SAFE_PATH = re.compile(r"^/[^\n\r\x00]*$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _self_container():
    if not containers.SELF_ID:
        raise NotPossible("LabKeel isn't running in a Docker container here, so it can't replace itself.")
    try:
        return containers.dclient().containers.get(containers.SELF_ID)
    except Exception as e:
        raise NotPossible(f"Couldn't ask Docker about LabKeel's own container ({type(e).__name__}).") from e


def detect():
    """(Blocking.) Everything needed to update this install, or NotPossible saying why it can't."""
    if not M().HOST_NS:
        raise NotPossible("LabKeel can't run commands on this machine (LABKEEL_HOST_COMMANDS is off), so it can't update itself.")
    c = _self_container()
    labels = c.attrs.get("Config", {}).get("Labels") or {}
    project = labels.get("com.docker.compose.project")
    workdir = labels.get("com.docker.compose.project.working_dir")
    service = labels.get("com.docker.compose.service")
    files = [f for f in (labels.get("com.docker.compose.project.config_files") or "").split(",") if f]
    if not (project and workdir and service and files):
        raise NotPossible("LabKeel wasn't started with Docker Compose, so there's no compose file to update it with.")
    if not (_SAFE_NAME.match(project) and _SAFE_NAME.match(service) and _SAFE_PATH.match(workdir)
            and all(_SAFE_PATH.match(f) for f in files)):
        raise NotPossible("LabKeel's compose project has a name or folder it won't pass to a command.")
    data_host = next((m.get("Source") for m in c.attrs.get("Mounts") or [] if m.get("Destination") == str(M().DATA_DIR)), None)
    if not data_host or not _SAFE_PATH.match(data_host):
        raise NotPossible("LabKeel's data folder isn't mounted from the machine, so an update couldn't report back.")
    if not _ok(host(["systemd-run", "--version"], 10)):
        raise NotPossible("This machine doesn't run systemd, which LabKeel uses to run the update outside its own container.")
    info = {"project": project, "workdir": workdir, "service": service, "files": files, "data_host": data_host,
            "image": (c.attrs.get("Config") or {}).get("Image"), "image_id": c.attrs.get("Image")}
    r = host(compose_args(info) + ["config", "--format", "json"], 30)
    if not _ok(r):
        raise NotPossible(f"docker compose couldn't read LabKeel's compose file: {_err(r)[0]}")
    try:
        svc = json.loads(r.stdout)["services"][service]
    except (ValueError, KeyError, TypeError) as e:
        raise NotPossible("docker compose's answer about LabKeel's service couldn't be read.") from e
    build = svc.get("build")
    if build:
        context = build.get("context") if isinstance(build, dict) else str(build)
        context = context or workdir
        st = host(["stat", "-c", "%U", context], 10)
        info["owner"] = _out(st) if _ok(st) else None
        top = host(as_owner(info, ["git", "-C", context, "rev-parse", "--show-toplevel"]), 15)
        if not _ok(top):
            raise NotPossible(f"LabKeel is built from {context}, but that isn't a git checkout, so there's nowhere to get updates from.")
        info.update(mode="source", repo=_out(top))
        up = host(as_owner(info, ["git", "-C", info["repo"], "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]), 15)
        if not _ok(up):
            raise NotPossible("The git checkout isn't following a branch (no upstream), so LabKeel can't tell where updates come from.")
        info["upstream"] = _out(up)
    else:
        if not updates.parse_ref(info["image"] or ""):
            raise NotPossible("LabKeel's image name couldn't be read.")
        info.update(mode="image", owner=None)
    return info


def install_info(fresh=False):
    """detect(), remembered for a few minutes. Returns (info, None) or (None, reason)."""
    with _lock:
        if not fresh and _state["info"] is not None and time.time() - _state["info_t"] < INFO_TTL:
            return _state["info"]
    try:
        got = (detect(), None)
    except NotPossible as e:
        got = (None, str(e))
    with _lock:
        _state["info"], _state["info_t"] = got, time.time()
    return got


# --------------------------------------------------------------------------- is there a newer version?
def _check_source(info):
    """(Blocking.) git fetch, then what's new on the branch we follow."""
    repo = info["repo"]
    git = lambda *a: as_owner(info, ["git", "-C", repo, *a])  # noqa: E731
    r = host(git("fetch", "--quiet"), FETCH_TIMEOUT)
    if not _ok(r):
        return {"status": "error", "detail": f"git fetch failed: {_err(r)[0]}"}
    here, there = host(git("rev-parse", "HEAD"), 15), host(git("rev-parse", "@{u}"), 15)
    behind = host(git("rev-list", "--count", "HEAD..@{u}"), 15)
    ahead = host(git("rev-list", "--count", "@{u}..HEAD"), 15)
    if not all(map(_ok, (here, there, behind, ahead))):
        return {"status": "error", "detail": "git couldn't compare this copy with the one it follows."}
    out = {"current": _out(here)[:12], "latest": _out(there)[:12], "behind": int(_out(behind) or 0),
           "ahead": int(_out(ahead) or 0), "changes": []}
    if out["behind"]:
        log = host(git("log", f"-{MAX_CHANGES}", "--format=%h%x09%cs%x09%s", "HEAD..@{u}"), 15)
        out["changes"] = [dict(zip(("id", "date", "text"), ln.split("\t", 2))) for ln in _out(log).splitlines() if ln.count("\t") >= 2]
        ver = host(git("show", "@{u}:VERSION"), 15)
        out["latest_version"] = version.read(_out(ver)) if _ok(ver) else None
    dirty = host(git("status", "--porcelain", "--untracked-files=no"), 15)
    out["local_changes"] = bool(_out(dirty)) if _ok(dirty) else False
    if out["ahead"]:
        out.update(status="blocked", detail=f"This copy has {out['ahead']} change{'s' if out['ahead'] != 1 else ''} of its own "
                                            "that aren't on the branch it follows, so git can't simply move it forward.")
    elif out["behind"] and out["local_changes"]:
        out.update(status="blocked", detail="Files in LabKeel's folder have been edited, so git won't update over them. "
                                            "Run git status in the folder to see which.")
    else:
        out["status"] = "update" if out["behind"] else "current"
    return out


async def _check_image(info):
    digests = await asyncio.to_thread(updates.repo_digests, containers.dclient(), {info["image_id"]})
    async with httpx.AsyncClient(timeout=updates.TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT},
                                 transport=updates._transport()) as http:
        row = await updates.check_image(http, info["image"], {info["image_id"]}, digests)
    out = {"current": (row["local_digest"] or "")[7:19] or None, "latest": (row["remote_digest"] or "")[7:19] or None,
           "behind": int(row["status"] == "update"), "changes": []}
    if row["status"] in ("update", "current"):
        out["status"] = row["status"]
    else:
        out.update(status="error", detail=row["detail"] or "The registry couldn't be asked.")
    return out


async def check(fresh=True):
    """Look for a newer LabKeel. Saves and returns the result. Raises NotPossible for an install that can't
    update itself."""
    info, why = await asyncio.to_thread(install_info, fresh)
    if not info:
        raise NotPossible(why)
    res = await (asyncio.to_thread(_check_source, info) if info["mode"] == "source" else _check_image(info))
    res.update(checked=time.time(), mode=info["mode"])
    d = await asyncio.to_thread(_load)
    old = d.get("check") or {}
    d["check"] = res
    await asyncio.to_thread(_save, d)
    if res["status"] == "update" and old.get("latest") != res["latest"]:
        await asyncio.to_thread(_announce, res)
    return res


def _announce(res):
    n = res.get("behind") or 0
    what = f"{n} new change{'s' if n != 1 else ''}" if res.get("mode") == "source" else "a newer image"
    U().log("update", f"A LabKeel update is ready: {what}. Install it from Settings → LabKeel.")
    if U().load_settings()["alerts"].get("updates"):
        U().notify("LabKeel update", f"A LabKeel update is ready ({what}). Install it from Settings → LabKeel.", "arrow_up")


async def scheduler():
    """Background task (see main.BACKGROUND_TASKS): looks for a newer LabKeel on the same schedule as the
    container update checks (Settings → Container updates)."""
    await asyncio.sleep(updates.START_DELAY + 60)
    while True:
        try:
            s = (await asyncio.to_thread(U().load_settings))["updates"]
            last = ((await asyncio.to_thread(_load)).get("check") or {}).get("checked") or 0
            if s["enabled"] and time.time() - last >= s["hours"] * 3600:
                await check()
        except asyncio.CancelledError:
            raise
        except NotPossible:
            pass   # nothing to check: Settings says why when someone looks
        except Exception as e:
            await asyncio.to_thread(U().log, "error", f"Checking for a LabKeel update failed: {type(e).__name__}: {e}")
        await asyncio.sleep(1800)


# --------------------------------------------------------------------------- installing it
def script(info):
    """The shell script systemd-run runs on the host. Every value in it is quoted; it's kept in the data
    folder (update.sh) so you can see exactly what ran."""
    q = shlex.quote
    log = q(f"{info['data_host']}/update.log")
    compose = " ".join(q(a) for a in compose_args(info))
    svc = q(info["service"])
    lines = [
        "#!/bin/sh",
        "# Written by LabKeel (selfupdate.py) to update itself. Safe to delete.",
        "set -u",
        f"exec >>{log} 2>&1",
        f"export PATH={q(HOST_PATH)} GIT_TERMINAL_PROMPT=0",
        f'step() {{ echo "\\$ $*"; "$@"; rc=$?; if [ "$rc" -ne 0 ]; then echo "{EXIT_MARK} $rc"; exit "$rc"; fi; }}',
        'echo "Started $(date -Is) on $(hostname)"',
        f"cd {q(info['workdir'])} || {{ echo '{EXIT_MARK} 1'; exit 1; }}",
    ]
    if info["mode"] == "source":
        lines.append("step " + " ".join(q(a) for a in as_owner(info, ["git", "-C", info["repo"], "pull", "--ff-only"])))
        lines.append(f"step {compose} up -d --build {svc}")
    else:
        lines.append(f"step {compose} pull {svc}")
        lines.append(f"step {compose} up -d {svc}")
    lines += ['echo "Finished $(date -Is)"', f"echo '{EXIT_MARK} 0'", ""]
    return "\n".join(lines)


def running():
    """The update in progress, if any: started, and its result not yet recorded (finish_if_done records it)."""
    p = _load().get("pending")
    return p if p and time.time() - p.get("t", 0) < GIVE_UP_AFTER else None


def start(info, who):
    """Write the script and hand it to systemd on the host. Returns the unit's name."""
    finish_if_done()  # a finished update not yet recorded doesn't count as running
    with _lock:
        d = _load()
        if d.get("pending") and running():
            raise HTTPException(409, "An update is already running.")
        unit = f"labkeel-update-{int(time.time())}"
        _data("update.sh").write_text(script(info))
        _data("update.log").write_text(f"LabKeel update to the latest {'commit' if info['mode'] == 'source' else 'image'}, "
                                       f"asked for by {who or 'someone'} (was version {VERSION}).\n")
        r = host(["systemd-run", "--unit", unit, "--collect", "--description", "LabKeel updating itself",
                  "/bin/sh", f"{info['data_host']}/update.sh"], 30)
        if not _ok(r):
            raise HTTPException(502, f"systemd couldn't start the update: {_err(r)[0]}")
        d["pending"] = {"t": time.time(), "unit": unit, "by": who, "from_version": VERSION,
                        "from": (d.get("check") or {}).get("current"), "mode": info["mode"]}
        _save(d)
    return unit


def finish_if_done():
    """If the update in progress has written its result, record how it went (once) and return it."""
    with _lock:
        d = _load()
        p = d.get("pending")
        if not p:
            return None
        tail = log_tail(400)
        code = _exit_code(tail)
        lost = code is None and time.time() - p.get("t", 0) > GIVE_UP_AFTER
        if code is None and not lost:
            return None
        ok = code == 0
        d["last"] = {"t": time.time(), "ok": ok, "by": p.get("by"), "from_version": p.get("from_version"),
                     "to_version": VERSION if ok else None, "detail": None if ok else _failure(tail, lost)}
        d.pop("pending", None)
        if ok:
            d.pop("check", None)  # whatever we knew about "newer" is out of date now
        _save(d)
    actor = sys.modules["security"].ACTOR
    token = actor.set({"user": p.get("by")} if p.get("by") else None)
    try:
        if ok:
            U().log("update", f"LabKeel updated (version {VERSION})")
        else:
            U().log("error", f"LabKeel update failed: {d['last']['detail']}")
            U().notify("LabKeel update failed", f"{d['last']['detail']} LabKeel is still running the old version. "
                                                "Settings → LabKeel shows what happened.", "warning")
    finally:
        actor.reset(token)
    with _lock:
        _state["info"] = None
    return d["last"]


def _failure(tail, lost):
    if lost:
        return "It didn't finish within an hour, so it's taken as failed."
    lines = [ln for ln in tail.splitlines() if ln.strip() and not ln.startswith(EXIT_MARK)]
    return (lines[-1][:300] + ".") if lines else "It stopped with an error."


def watch():
    """Background thread: while an update runs, check every few seconds whether it has finished. Started on
    start-up (the new LabKeel finishing the job) and when an update is started (in case it fails before
    LabKeel is replaced)."""
    with _lock:
        if _state["watching"]:
            return
        _state["watching"] = True
    try:
        while _load().get("pending"):
            if finish_if_done():
                break
            time.sleep(3)
    except Exception as e:
        print(f"[update] watching the update failed: {e}", flush=True)
    finally:
        with _lock:
            _state["watching"] = False


def resume():
    """Called on start-up: pick up an update that was in progress when LabKeel last stopped."""
    if _load().get("pending"):
        threading.Thread(target=watch, daemon=True, name="selfupdate.watch").start()


# --------------------------------------------------------------------------- the API (admins only)
@router.get("/api/labkeel/update")
def api_update_state(fresh: bool = False):
    finish_if_done()  # so the page never sees "finished" before the result is recorded
    info, why = install_info(fresh)
    d = _load()
    return {"version": VERSION, "possible": bool(info), "reason": why,
            "install": info and {k: info.get(k) for k in ("mode", "workdir", "service", "project", "repo", "upstream", "image")},
            "check": d.get("check"), "running": running(), "last": d.get("last"),
            "log": log_tail() if (running() or d.get("last")) else ""}


@router.post("/api/labkeel/update/check")
async def api_update_check():
    try:
        return await check()
    except NotPossible as e:
        raise HTTPException(409, str(e)) from e


@router.post("/api/labkeel/update/install")
def api_update_install(request: Request, body: dict = Body(...)):
    """Install the update (password again). LabKeel restarts at the end, so the page waits and reloads."""
    if body.get("confirm") != "update":
        raise HTTPException(400, "Missing confirmation")
    info, why = install_info(fresh=True)
    if not info:
        raise HTTPException(409, why)
    chk = _load().get("check") or {}
    if chk.get("status") == "blocked":
        raise HTTPException(409, chk.get("detail") or "This copy can't be updated automatically.")
    if chk.get("status") != "update":
        raise HTTPException(409, "There's no update to install. Check for one first.")
    sec = sys.modules["security"]
    sec.confirm_password(request, body.get("password"))
    who = (sec.ACTOR.get() or {}).get("user")
    unit = start(info, who)
    U().log("update", f"Updating LabKeel from version {VERSION} ({'git pull and rebuild' if info['mode'] == 'source' else 'new image'})")
    ctx = contextvars.copy_context()
    threading.Thread(target=ctx.run, args=(watch,), daemon=True, name="selfupdate.watch").start()
    return {"ok": True, "unit": unit}
