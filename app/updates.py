"""
Image update checks: is a newer image out for what each container runs?

For each image a container uses, LabKeel asks the image's registry which digest its tag points to now, and
compares that with the digest the container's image was pulled as. It's one HEAD request per image (these
don't count towards Docker Hub's pull limit), made anonymously, a few at a time. Nothing is downloaded or
changed: updating stays your call (`docker compose pull` and `docker compose up -d`).

Works with Docker Hub, GHCR, lscr.io, Quay and any registry that follows the OCI distribution spec and lets
anyone read public images with a token from its own sign-in service.

Each image ends up with one status:
  current  the container runs what the tag points to now
  update   the tag points to a newer image than the container runs
  pinned   the container names an exact digest (image@sha256:…), so it never changes by itself
  local    built or loaded on this machine, not pulled from a registry: nothing to compare with
  unknown  the registry won't show it without a login: a private image, or (with Docker's containerd image
           store, which records a digest even for local builds) one that only exists on this machine
  error    the check failed (registry down, tag deleted, rate limited…); `detail` says which

Results are kept in the database (table image_checks, see db.py), so they survive restarts and the page
reads them without waiting on any registry. The check runs on a schedule (Settings → Container updates) as
an asyncio task, and on demand from the Containers page.
"""
import asyncio
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, HTTPException

import containers
import db
from version import USER_AGENT

router = APIRouter()

CONCURRENCY = 4              # registries asked at once
TIMEOUT = httpx.Timeout(15.0, connect=10.0)
MAX_IMAGES = 300             # per check; far more than a homelab runs
MAX_BODY = 1 << 20           # the most we read of any registry answer (tokens and manifests are a few KB)
START_DELAY = 120            # seconds after start-up before the first scheduled check
RETRY_AFTER_FAILURE = 1800   # a scheduled check that failed outright is tried again after this long
MANUAL_WAIT = 90             # how long "Check now" waits for an answer before saying it's still going
ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
))
STATUSES = ("current", "update", "pinned", "local", "unknown", "error")   # as the database's CHECK allows
NEEDS_LOGIN = ("The registry won't show this image without a login: it's private, or it was only ever built "
               "on this machine")


def U():
    """The upkeep module (settings, activity log, alerts), looked up when needed."""
    return sys.modules["upkeep"]


# --------------------------------------------------------------------------- image names
@dataclass(frozen=True)
class Ref:
    """An image name, split up the way Docker reads it."""
    registry: str          # "docker.io", "ghcr.io", "lscr.io", "registry.example.com:5000"
    repo: str              # "library/nginx", "linuxserver/plex"
    tag: str | None        # "latest", "1.27-alpine"; None when only a digest is given
    digest: str | None     # "sha256:…" when the name pins one

    @property
    def api_host(self):
        """Where the registry's API answers (Docker Hub's isn't at docker.io)."""
        return "registry-1.docker.io" if self.registry == "docker.io" else self.registry


_HOST = re.compile(r"^(?:localhost|[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*)(?::\d{1,5})?$")
_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_REPO = re.compile(rf"^{_COMPONENT}(?:/{_COMPONENT})*$")
_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_DOCKER_HUB = ("docker.io", "index.docker.io", "registry-1.docker.io", "registry.hub.docker.com")


def parse_ref(text):
    """'nginx' → Ref('docker.io', 'library/nginx', 'latest', None). None if it isn't a valid image name.

    The first part is a registry only if it has a dot or a port, or is 'localhost' (Docker's own rule), so
    'linuxserver/plex' is on Docker Hub and 'ghcr.io/home-assistant/home-assistant:stable' is on GHCR."""
    if not isinstance(text, str) or not 0 < len(text) <= 512:
        return None
    rest, _, digest = text.partition("@")
    if digest and not _DIGEST.match(digest):
        return None
    first, slash, remainder = rest.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        registry, path = first.lower(), remainder
    else:
        registry, path = "docker.io", rest
    tag = None
    if ":" in path.rsplit("/", 1)[-1]:
        path, tag = path.rsplit(":", 1)
    if registry in _DOCKER_HUB:
        registry = "docker.io"
        if "/" not in path:
            path = "library/" + path   # official images: 'nginx' is 'library/nginx'
    if not _HOST.match(registry) or not _REPO.match(path) or (tag is not None and not _TAG.match(tag)):
        return None
    if tag is None and not digest:
        tag = "latest"
    return Ref(registry, path, tag, digest or None)


def local_digests(repo_digests, ref):
    """The digests an image was pulled as, from the same registry and repository as `ref`.
    (Docker keeps them in the image's RepoDigests: ['nginx@sha256:…', ...].)"""
    out = set()
    for rd in repo_digests or ():
        name, _, digest = str(rd).partition("@")
        r = parse_ref(name)
        if r and (r.registry, r.repo) == (ref.registry, ref.repo) and _DIGEST.match(digest):
            out.add(digest)
    return out


# --------------------------------------------------------------------------- asking the registry
class DockerUnavailable(Exception):
    """Docker couldn't be asked which images are in use, so nothing was checked."""


class CheckFailed(Exception):
    """The registry couldn't tell us. `status` is 'unknown' (needs a login) or 'error'; `detail` is for people."""

    def __init__(self, status, detail):
        super().__init__(detail)
        self.status, self.detail = status, detail


async def _read_capped(resp):
    """A response body, refusing anything bigger than MAX_BODY (a registry has no reason to send more)."""
    chunks, size = [], 0
    async for chunk in resp.aiter_bytes():
        size += len(chunk)
        if size > MAX_BODY:
            raise CheckFailed("error", "The registry sent far more than expected, so its answer was ignored")
        chunks.append(chunk)
    return b"".join(chunks)


_PARAM = re.compile(r'(\w+)="([^"]*)"')


async def _token(http, challenge, ref):
    """An anonymous pull token, from the sign-in service the registry's 401 answer names. None if the registry
    wants a password (Basic) or the service won't give one."""
    scheme, _, params = challenge.partition(" ")
    if scheme.lower() != "bearer":
        return None
    p = dict(_PARAM.findall(params))
    realm = p.get("realm", "")
    if not realm.startswith("https://"):
        return None  # never follow a registry's hint to plain HTTP (or anywhere that isn't a web address)
    params = {"scope": p.get("scope") or f"repository:{ref.repo}:pull"}
    if p.get("service"):
        params["service"] = p["service"]
    async with http.stream("GET", realm, params=params) as r:
        if r.status_code != 200:
            return None
        body = await _read_capped(r)
    try:
        data = json.loads(body)
    except ValueError:
        return None
    tok = (data.get("token") or data.get("access_token")) if isinstance(data, dict) else None
    return tok if isinstance(tok, str) and 0 < len(tok) < 16384 else None


async def remote_digest(http, ref):
    """The digest `ref`'s tag points to now. Raises CheckFailed if the registry can't say.

    A HEAD request first, signing in anonymously if asked. A registry may send us to another (lscr.io sends
    you to GHCR) and that one may want its own token, so this follows up to three sign-ins, once per host."""
    url = f"https://{ref.api_host}/v2/{ref.repo}/manifests/{ref.tag}"
    headers = {"Accept": ACCEPT}
    tried = set()
    for _ in range(3):
        r = await http.head(url, headers=headers)
        if r.status_code != 401:
            break
        host = r.url.host
        if host in tried:
            raise CheckFailed("unknown", NEEDS_LOGIN)
        tried.add(host)
        token = await _token(http, r.headers.get("www-authenticate", ""), ref)
        if not token:
            raise CheckFailed("unknown", NEEDS_LOGIN)
        headers = {"Accept": ACCEPT, "Authorization": f"Bearer {token}"}
        url = str(r.url)  # the registry that actually answered, after any redirect
    code = r.status_code
    if code in (401, 403):
        raise CheckFailed("unknown", NEEDS_LOGIN)
    if code == 404:
        raise CheckFailed("error", f"The registry has no '{ref.tag}' tag for this image any more")
    if code == 429:
        raise CheckFailed("error", "The registry asked LabKeel to slow down (rate limited). The next check will try again")
    if code >= 500:
        raise CheckFailed("error", f"The registry is having trouble (it answered {code})")
    digest = r.headers.get("docker-content-digest", "") if code == 200 else ""
    if not _DIGEST.match(digest):
        # Some registries leave the digest out of HEAD answers (or don't answer HEAD at all): fetch the
        # manifest itself. Its digest is the SHA-256 of exactly the bytes sent.
        async with http.stream("GET", str(r.url) if code == 200 else url, headers=headers) as g:
            if g.status_code != 200:
                raise CheckFailed("error", f"The registry answered {g.status_code}")
            body = await _read_capped(g)
        digest = g.headers.get("docker-content-digest", "")
        if not _DIGEST.match(digest):
            digest = "sha256:" + hashlib.sha256(body).hexdigest()
    return digest


# --------------------------------------------------------------------------- one image, all images
def _inventory():
    """(Blocking: run in a thread.) What each container runs, and every local image's digests.
    Returns ({image name: {image IDs}}, {image ID: [RepoDigests]})."""
    cl = containers.dclient()
    by_name = {}
    for c in cl.containers.list(all=True):
        name = ((c.attrs.get("Config") or {}).get("Image") or "").strip()
        if name:
            by_name.setdefault(name, set()).add(c.attrs.get("Image"))
    digests = {i["Id"]: i.get("RepoDigests") or [] for i in cl.api.images()}
    return by_name, digests


async def check_image(http, name, image_ids, digests):
    """Check one image name. `image_ids` are the images its containers were made from; `digests` maps image IDs
    to their RepoDigests. Returns a row for image_checks. Never raises for registry trouble."""
    row = {"image": name, "status": "error", "local_digest": None, "remote_digest": None, "detail": None,
           "checked": time.time()}
    ref = parse_ref(name)
    if ref is None:
        return {**row, "detail": "LabKeel can't read this image name"}
    if ref.digest:
        return {**row, "status": "pinned", "local_digest": ref.digest,
                "detail": "Fixed to one exact build (name@sha256:…), so it never changes by itself"}
    mine = {i: local_digests(digests.get(i), ref) for i in image_ids}
    have = set().union(*mine.values()) if mine else set()
    if not have:
        return {**row, "status": "local",
                "detail": "Built or loaded on this machine rather than pulled from a registry, so there's nothing to compare with"}
    row["local_digest"] = sorted(have)[0]
    try:
        remote = await remote_digest(http, ref)
    except CheckFailed as e:
        return {**row, "status": e.status, "detail": e.detail}
    except httpx.TimeoutException:
        return {**row, "detail": f"{ref.api_host} didn't answer in time"}
    except httpx.HTTPError as e:
        return {**row, "detail": f"Couldn't reach {ref.api_host} ({type(e).__name__})"}
    behind = any(remote not in d for d in mine.values() if d)  # a container still on an older pull
    return {**row, "status": "update" if behind else "current", "remote_digest": remote}


def _save(rows):
    """Replace the stored results with these (images no longer used are dropped). Returns the old ones."""
    with db.transaction() as con:
        old = {r["image"]: dict(r) for r in con.execute("SELECT * FROM image_checks")}
        con.execute("DELETE FROM image_checks")
        con.executemany("INSERT INTO image_checks (image, status, local_digest, remote_digest, detail, checked) "
                        "VALUES (:image, :status, :local_digest, :remote_digest, :detail, :checked)", rows)
    return old


def newly_found(old, rows):
    """Images with an update that wasn't already reported (a new one, or a newer one than last time)."""
    return [r["image"] for r in rows if r["status"] == "update" and not (
        old.get(r["image"], {}).get("status") == "update" and old[r["image"]].get("remote_digest") == r["remote_digest"])]


async def check_all():
    """Check every image a container uses, a few registries at a time. Saves the results, logs and alerts
    about new updates. Returns a summary for the page."""
    _state["tried"] = time.time()
    try:
        by_name, digests = await asyncio.to_thread(_inventory)
    except Exception as e:
        raise DockerUnavailable(f"can't reach Docker ({type(e).__name__}: {e})") from e
    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT},
                                 transport=_transport()) as http:
        async def one(name, ids):
            async with sem:
                return await check_image(http, name, ids, digests)
        rows = list(await asyncio.gather(*(one(n, ids) for n, ids in sorted(by_name.items())[:MAX_IMAGES])))
    old = await asyncio.to_thread(_save, rows)
    _state["finished"] = time.time()
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in STATUSES}
    new = newly_found(old, rows)
    manual = bool((sys.modules["security"].ACTOR.get() or {}).get("user"))
    await asyncio.to_thread(_report, rows, counts, new, manual)
    return {"checked": len(rows), "counts": counts, "new": new}


def _report(rows, counts, new, manual):
    """Activity log and alerts for one check (blocking: run in a thread)."""
    if manual:
        n = counts["update"]
        U().log("update", f"Checked {len(rows)} image{'s' if len(rows) != 1 else ''} for updates: "
                          + (f"{n} {'has' if n == 1 else 'have'} a newer version" if n else "all up to date")
                          + (f"; {counts['error']} couldn't be checked" if counts["error"] else ""))
    if not new:
        return
    names = ", ".join(new[:8]) + (f" and {len(new) - 8} more" if len(new) > 8 else "")
    if not manual:
        U().log("update", f"Newer images are out for {names}")
    if U().load_settings()["alerts"].get("updates"):
        U().notify("Container updates", f"Newer images are out for {names}. Pull them and re-create the containers "
                                        "to use them (docker compose pull, then docker compose up -d).", "arrow_up")


def _transport():
    """The HTTP transport for registries: the default, except in tests (which swap in a fake registry)."""
    return None


# --------------------------------------------------------------------------- running it: on demand, on a schedule
_state = {"task": None, "tried": 0.0, "finished": 0.0}


def checking():
    t = _state["task"]
    return t is not None and not t.done()


async def check_now():
    """Run a check, or join the one already running (two never run at once)."""
    loop = asyncio.get_running_loop()
    t = _state["task"]
    if t is None or t.done() or t.get_loop() is not loop:
        t = _state["task"] = loop.create_task(check_all(), name="updates.check_all")
    return await asyncio.shield(t)


def last_checked():
    row = db.query("SELECT MAX(checked) AS t FROM image_checks")
    return max(row[0]["t"] or 0.0, _state["finished"])


def due(now=None):
    """Whether a scheduled check should run now (blocking: reads the settings and the database)."""
    s = U().load_settings()["updates"]
    now = now or time.time()
    return (s["enabled"] and now - last_checked() >= s["hours"] * 3600
            and now - _state["tried"] >= RETRY_AFTER_FAILURE)


async def scheduler():
    """Runs for as long as LabKeel does (see main.BACKGROUND_TASKS). Looks every 10 minutes whether a check
    is due (Settings → Container updates), so a changed setting applies without a restart."""
    await asyncio.sleep(START_DELAY)
    while True:
        try:
            if await asyncio.to_thread(due):
                await check_now()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # keep going: Docker or the network may be back next time
            await asyncio.to_thread(U().log, "error", f"Checking for image updates failed: {type(e).__name__}: {e}")
        await asyncio.sleep(600)


# --------------------------------------------------------------------------- what each container gets
_img_cache = {"t": 0.0, "digests": {}}


def _image_digests(need=()):
    """Local images' RepoDigests by ID, read at most once a minute, or sooner for an image not seen yet
    (a container re-created on a freshly pulled image). Empty if Docker can't be reached."""
    c = _img_cache
    now = time.time()
    if now - c["t"] > 60 or (now - c["t"] > 5 and any(i not in c["digests"] for i in need)):
        try:
            c["digests"] = {i["Id"]: i.get("RepoDigests") or [] for i in containers.dclient().api.images()}
        except Exception:
            pass
        c["t"] = now
    return c["digests"]


def container_status(row, image_id, digests):
    """One container's update status, from its image's latest check. Worked out per container because two
    containers using the same name may run different builds (one re-created since a pull, one not)."""
    if row["status"] not in ("current", "update") or not row["remote_digest"]:
        return {"status": row["status"], "detail": row["detail"]}
    ref = parse_ref(row["image"])
    if image_id in digests:
        mine = local_digests(digests[image_id], ref)
        if row["remote_digest"] in mine:
            return {"status": "current", "detail": None}
        pulled = any(row["remote_digest"] in local_digests(d, ref) for d in digests.values())
        return {"status": "update", "pulled": pulled,
                "detail": "The newer image is already downloaded: re-create the container to use it" if pulled else
                          "A newer image is out: pull it, then re-create the container"}
    return {"status": row["status"], "detail": None}


@router.get("/api/updates")
def api_updates():
    """The latest results: per image, and per container (keyed by the short ID the Containers page uses)."""
    rows = {r["image"]: r for r in db.query("SELECT image, status, local_digest, remote_digest, detail, checked FROM image_checks")}
    items = containers.state["items"]
    digests = _image_digests({c.get("image_id") for c in items})
    per = {}
    for c in items:
        row = rows.get(c.get("image"))
        if row:
            per[c["id"]] = container_status(row, c.get("image_id"), digests)
    s = U().load_settings()["updates"]
    return {"enabled": s["enabled"], "hours": s["hours"], "checking": checking(),
            "last": max((r["checked"] for r in rows.values()), default=None),
            "count": sum(1 for v in per.values() if v["status"] == "update"),
            "images": sorted(rows.values(), key=lambda r: r["image"]), "containers": per}


@router.post("/api/updates/check")
async def api_updates_check():
    """Check now (admins). Waits for the answer, up to MANUAL_WAIT seconds; after that it carries on in the
    background and the page picks the results up from GET /api/updates."""
    try:
        return {"done": True, **await asyncio.wait_for(asyncio.shield(check_now()), MANUAL_WAIT)}
    except TimeoutError:
        return {"done": False}
    except DockerUnavailable as e:
        raise HTTPException(503, f"Couldn't check for updates: {e}") from e
