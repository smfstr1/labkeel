"""
The Apps page: add a popular self-hosted app in a couple of clicks.

LabKeel does what you'd do by hand. For an app it:

  1. writes a compose file into the app's own folder on the machine (LABKEEL_APPS_DIR/<app>, so
     /srv/labkeel/apps/jellyfin by default), from a template below filled in with your choices
  2. runs `docker compose pull` and `docker compose up -d` there
  3. waits until it answers on its port (or, for an app with no web page, until its container is running)
  4. adds it to the Services page (kept in data/apps.json: your config.json is never rewritten)

Everything it made is ordinary Docker Compose: the folder can be managed by hand afterwards, and removing an
app from here runs `docker compose down` and leaves the folder and its data alone.

Adding apps can give a container full control of the machine (Home Assistant asks for that), so it's off
unless LABKEEL_ALLOW_APP_INSTALLS=1, admins only, the password again, and every step is in the activity log.
"""
import contextvars
import json
import re
import secrets
import string
import sys
import threading
import time
from pathlib import PurePosixPath

from fastapi import APIRouter, Body, HTTPException, Request

import containers
import env
import selfupdate

router = APIRouter()

ALLOWED = env.flag("LABKEEL_ALLOW_APP_INSTALLS")
APPS_DIR = env.text("LABKEEL_APPS_DIR", "/srv/labkeel/apps").rstrip("/") or "/srv/labkeel/apps"
PULL_TIMEOUT = 1800    # big images on a slow line
UP_TIMEOUT = 300
ANSWER_WAIT = 180      # seconds an app has to start answering on its port

_jobs = {}               # app folder name -> its latest add or remove (what the page shows)
_lock = threading.Lock()


def M():
    return sys.modules["main"]


def U():
    return sys.modules["upkeep"]


# --------------------------------------------------------------------------- the catalogue
# Each app: what it is, its image, the ports and folders it needs, and anything else for its compose file.
#   ports:   [{"name", "container", "default", "proto"?, "web"?, "fixed"?, "follow"?, "same_as"?}]
#            web = its web page. fixed = can't be changed (the app needs that number, or uses the machine's
#            network). follow = the app is told the number, so it's the same inside the container and out.
#            same_as = the same number as an earlier port (TCP and UDP on one number).
#   folders: [{"key", "label", "to", "default", "ro"?, "optional"?}]  default: "{dir}" is the app's folder
#   fields:  [{"key", "label", "default"?, "secret"?, "optional"?, "help"?}] → environment variables
#   web_path: added to the link (Plex's page is at /web)
#   host_network: uses the machine's network directly (finds devices, needs many ports)
#   extra:   more compose settings for the main service, as they are
#   detect:  image names meaning it's already running here (added some other way)
LS = {"PUID": "{uid}", "PGID": "{gid}", "TZ": "{tz}"}  # what every linuxserver.io image takes
CATALOGUE = [
    {"id": "jellyfin", "name": "Jellyfin", "category": "Media", "image": "jellyfin/jellyfin:latest",
     "description": "Stream your films, shows and music to any device. Free and open source.",
     "ports": [{"name": "Web page", "container": 8096, "default": 8096, "web": True}],
     "folders": [{"key": "config", "label": "Settings folder", "to": "/config", "default": "{dir}/config"},
                 {"key": "cache", "label": "Cache folder", "to": "/cache", "default": "{dir}/cache"},
                 {"key": "media", "label": "Media folder", "to": "/media", "default": "", "ro": True}],
     "env": {"TZ": "{tz}"}, "detect": ["jellyfin/jellyfin", "lscr.io/linuxserver/jellyfin", "linuxserver/jellyfin"],
     "next": "Open it to create your account and add your libraries (they're under /media)."},
    {"id": "plex", "name": "Plex", "category": "Media", "image": "lscr.io/linuxserver/plex:latest",
     "description": "Stream your media library at home and away, with apps for every TV and phone.",
     "host_network": True, "web_path": "/web",
     "ports": [{"name": "Web page", "container": 32400, "default": 32400, "web": True, "fixed": True}],
     "folders": [{"key": "config", "label": "Settings folder", "to": "/config", "default": "{dir}/config"},
                 {"key": "media", "label": "Media folder", "to": "/media", "default": "", "ro": True}],
     "fields": [{"key": "PLEX_CLAIM", "label": "Claim code", "optional": True, "secret": True,
                 "help": "From plex.tv/claim, so the server joins your Plex account. It only lasts 4 minutes."}],
     "env": {**LS, "VERSION": "docker"},
     "detect": ["plexinc/pms-docker", "lscr.io/linuxserver/plex", "linuxserver/plex"],
     "next": "Open it to sign in and add your libraries (they're under /media)."},
    {"id": "immich", "name": "Immich", "category": "Photos", "upstream": "immich",
     "description": "Back up the photos and videos on your phone, with faces, places and search.",
     "ports": [{"name": "Web page", "container": 2283, "default": 2283, "web": True}],
     "folders": [{"key": "UPLOAD_LOCATION", "label": "Photos folder", "to": "", "default": "{dir}/library"},
                 {"key": "DB_DATA_LOCATION", "label": "Database folder (on a local drive, not a network share)",
                  "to": "", "default": "{dir}/postgres"}],
     "detect": ["ghcr.io/immich-app/immich-server", "altran1502/immich-server"],
     "next": "Open it to create the admin account, then install the Immich app on your phone."},
    {"id": "home-assistant", "name": "Home Assistant", "category": "Home",
     "image": "ghcr.io/home-assistant/home-assistant:stable",
     "description": "Lights, heating and sensors in one place, with automations.",
     "host_network": True,
     "ports": [{"name": "Web page", "container": 8123, "default": 8123, "web": True, "fixed": True}],
     "folders": [{"key": "config", "label": "Settings folder", "to": "/config", "default": "{dir}/config"}],
     "env": {"TZ": "{tz}"},
     "extra": {"privileged": True, "volumes": ["/etc/localtime:/etc/localtime:ro", "/run/dbus:/run/dbus:ro"]},
     "full_control": True, "detect": ["ghcr.io/home-assistant/home-assistant", "homeassistant/home-assistant"],
     "next": "Open it to name your home and create an account. It finds devices on your network by itself."},
    {"id": "sonarr", "name": "Sonarr", "category": "Downloads", "image": "lscr.io/linuxserver/sonarr:latest",
     "description": "Finds, downloads and sorts new episodes of the shows you follow.",
     "ports": [{"name": "Web page", "container": 8989, "default": 8989, "web": True}],
     "folders": [{"key": "config", "label": "Settings folder", "to": "/config", "default": "{dir}/config"},
                 {"key": "tv", "label": "TV shows folder", "to": "/tv", "default": ""},
                 {"key": "downloads", "label": "Downloads folder", "to": "/downloads", "default": ""}],
     "env": LS, "detect": ["lscr.io/linuxserver/sonarr", "linuxserver/sonarr"],
     "next": "Open it to add your download client and an indexer (or Prowlarr)."},
    {"id": "radarr", "name": "Radarr", "category": "Downloads", "image": "lscr.io/linuxserver/radarr:latest",
     "description": "Sonarr for films: finds, downloads and sorts them for you.",
     "ports": [{"name": "Web page", "container": 7878, "default": 7878, "web": True}],
     "folders": [{"key": "config", "label": "Settings folder", "to": "/config", "default": "{dir}/config"},
                 {"key": "movies", "label": "Films folder", "to": "/movies", "default": ""},
                 {"key": "downloads", "label": "Downloads folder", "to": "/downloads", "default": ""}],
     "env": LS, "detect": ["lscr.io/linuxserver/radarr", "linuxserver/radarr"],
     "next": "Open it to add your download client and an indexer (or Prowlarr)."},
    {"id": "prowlarr", "name": "Prowlarr", "category": "Downloads", "image": "lscr.io/linuxserver/prowlarr:latest",
     "description": "Keeps the indexers for Sonarr and Radarr in one place.",
     "ports": [{"name": "Web page", "container": 9696, "default": 9696, "web": True}],
     "folders": [{"key": "config", "label": "Settings folder", "to": "/config", "default": "{dir}/config"}],
     "env": LS, "detect": ["lscr.io/linuxserver/prowlarr", "linuxserver/prowlarr"]},
    {"id": "qbittorrent", "name": "qBittorrent", "category": "Downloads", "image": "lscr.io/linuxserver/qbittorrent:latest",
     "description": "A download client with a web page, for Sonarr and Radarr to send downloads to.",
     "ports": [{"name": "Web page", "container": 8080, "default": 8080, "web": True, "follow": True},
               {"name": "Downloads", "container": 6881, "default": 6881, "follow": True},
               {"name": "Downloads (UDP)", "container": 6881, "default": 6881, "proto": "udp", "follow": True, "same_as": 1}],
     "folders": [{"key": "config", "label": "Settings folder", "to": "/config", "default": "{dir}/config"},
                 {"key": "downloads", "label": "Downloads folder", "to": "/downloads", "default": ""}],
     "env": {**LS, "WEBUI_PORT": "{port0}", "TORRENTING_PORT": "{port1}"},
     "detect": ["lscr.io/linuxserver/qbittorrent", "linuxserver/qbittorrent"],
     "next": "Its first password is in its log (Containers → qbittorrent → Logs). Change it straight away."},
    {"id": "jellyseerr", "name": "Jellyseerr", "category": "Media", "image": "fallenbagel/jellyseerr:latest",
     "description": "Lets family and friends ask for films and shows, then sends them to Sonarr and Radarr.",
     "ports": [{"name": "Web page", "container": 5055, "default": 5055, "web": True}],
     "folders": [{"key": "config", "label": "Settings folder", "to": "/app/config", "default": "{dir}/config"}],
     "env": {"TZ": "{tz}"}, "detect": ["fallenbagel/jellyseerr"]},
    {"id": "audiobookshelf", "name": "Audiobookshelf", "category": "Media", "image": "ghcr.io/advplyr/audiobookshelf:latest",
     "description": "Audiobooks and podcasts, with apps that remember where you got to.",
     "ports": [{"name": "Web page", "container": 80, "default": 13378, "web": True}],
     "folders": [{"key": "config", "label": "Settings folder", "to": "/config", "default": "{dir}/config"},
                 {"key": "metadata", "label": "Covers and caches folder", "to": "/metadata", "default": "{dir}/metadata"},
                 {"key": "audiobooks", "label": "Audiobooks folder", "to": "/audiobooks", "default": ""},
                 {"key": "podcasts", "label": "Podcasts folder", "to": "/podcasts", "default": "{dir}/podcasts"}],
     "env": {"TZ": "{tz}"}, "detect": ["ghcr.io/advplyr/audiobookshelf", "advplyr/audiobookshelf"]},
    {"id": "navidrome", "name": "Navidrome", "category": "Media", "image": "deluan/navidrome:latest",
     "description": "Your music collection, streamed to the browser and to Subsonic apps on your phone.",
     "ports": [{"name": "Web page", "container": 4533, "default": 4533, "web": True}],
     "folders": [{"key": "data", "label": "Settings folder", "to": "/data", "default": "{dir}/data"},
                 {"key": "music", "label": "Music folder", "to": "/music", "default": "", "ro": True}],
     "detect": ["deluan/navidrome"]},
    {"id": "paperless", "name": "Paperless-ngx", "category": "Tools", "image": "ghcr.io/paperless-ngx/paperless-ngx:latest",
     "description": "Scan your post and paperwork, then find any of it by searching its text.",
     "ports": [{"name": "Web page", "container": 8000, "default": 8000, "web": True}],
     "folders": [{"key": "data", "label": "Database folder", "to": "/usr/src/paperless/data", "default": "{dir}/data"},
                 {"key": "media", "label": "Documents folder", "to": "/usr/src/paperless/media", "default": "{dir}/media"},
                 {"key": "consume", "label": "Drop-in folder (new scans go here)", "to": "/usr/src/paperless/consume",
                  "default": "{dir}/consume"},
                 {"key": "export", "label": "Export folder", "to": "/usr/src/paperless/export", "default": "{dir}/export"}],
     "env": {"PAPERLESS_REDIS": "redis://broker:6379", "PAPERLESS_TIME_ZONE": "{tz}", "USERMAP_UID": "{uid}",
             "USERMAP_GID": "{gid}"},
     "extra": {"depends_on": ["broker"]},
     "services": {"broker": {"image": "docker.io/library/redis:7", "restart": "unless-stopped",
                             "volumes": ["{dir}/redis:/data"]}},
     "detect": ["ghcr.io/paperless-ngx/paperless-ngx", "paperlessngx/paperless-ngx"],
     "next": "Open it to create the first account."},
    {"id": "nextcloud", "name": "Nextcloud", "category": "Files", "image": "lscr.io/linuxserver/nextcloud:latest",
     "description": "Your own cloud: files, calendars and contacts, synced to every device.",
     "web_scheme": "https",
     "ports": [{"name": "Web page", "container": 443, "default": 8443, "web": True}],
     "folders": [{"key": "config", "label": "Settings folder", "to": "/config", "default": "{dir}/config"},
                 {"key": "data", "label": "Files folder", "to": "/data", "default": "{dir}/data"}],
     "env": LS, "detect": ["lscr.io/linuxserver/nextcloud", "linuxserver/nextcloud", "nextcloud"],
     "next": "Open it to create the admin account. Its own certificate is self-signed, so your browser will warn once."},
    {"id": "syncthing", "name": "Syncthing", "category": "Files", "image": "lscr.io/linuxserver/syncthing:latest",
     "description": "Keeps folders in step between your computers and phones, without a cloud in the middle.",
     "ports": [{"name": "Web page", "container": 8384, "default": 8384, "web": True},
               {"name": "Syncing (TCP)", "container": 22000, "default": 22000, "fixed": True},
               {"name": "Syncing (UDP)", "container": 22000, "default": 22000, "proto": "udp", "fixed": True, "same_as": 1},
               {"name": "Finding devices", "container": 21027, "default": 21027, "proto": "udp", "fixed": True}],
     "folders": [{"key": "config", "label": "Settings folder", "to": "/config", "default": "{dir}/config"},
                 {"key": "data", "label": "Folder to sync", "to": "/data1", "default": ""}],
     "env": LS, "detect": ["lscr.io/linuxserver/syncthing", "linuxserver/syncthing", "syncthing/syncthing"]},
    {"id": "vaultwarden", "name": "Vaultwarden", "category": "Tools", "image": "vaultwarden/server:latest",
     "description": "A small password manager server for the Bitwarden apps and browser extensions.",
     "ports": [{"name": "Web page", "container": 80, "default": 8222, "web": True}],
     "folders": [{"key": "data", "label": "Data folder", "to": "/data", "default": "{dir}/data"}],
     "detect": ["vaultwarden/server"],
     "next": "Browsers only let it work over HTTPS: reach it through a reverse proxy or Tailscale before you use it."},
    {"id": "uptime-kuma", "name": "Uptime Kuma", "category": "Tools", "image": "louislam/uptime-kuma:2",
     "description": "Watches websites and services, and tells you when one goes down.",
     "ports": [{"name": "Web page", "container": 3001, "default": 3001, "web": True}],
     "folders": [{"key": "data", "label": "Data folder", "to": "/app/data", "default": "{dir}/data"}],
     "detect": ["louislam/uptime-kuma"]},
    {"id": "pihole", "name": "Pi-hole", "category": "Network", "image": "pihole/pihole:latest",
     "description": "Blocks adverts and trackers for every device on your network.",
     "web_path": "/admin",
     "ports": [{"name": "Web page", "container": 80, "default": 8053, "web": True},
               {"name": "DNS (TCP)", "container": 53, "default": 53, "fixed": True},
               {"name": "DNS (UDP)", "container": 53, "default": 53, "proto": "udp", "fixed": True, "same_as": 1}],
     "folders": [{"key": "config", "label": "Settings folder", "to": "/etc/pihole", "default": "{dir}/etc-pihole"}],
     "fields": [{"key": "FTLCONF_webserver_api_password", "label": "Web page password", "secret": True}],
     "env": {"TZ": "{tz}"}, "detect": ["pihole/pihole"],
     "next": "Point your router's DNS setting at this machine. On Ubuntu, port 53 may need freeing first "
             "(DNSStubListener=no in /etc/systemd/resolved.conf)."},
    {"id": "minecraft-bedrock", "name": "Minecraft Bedrock", "category": "Games", "image": "itzg/minecraft-bedrock-server:latest",
     "description": "A Minecraft world for friends and family on phones, consoles and PCs.",
     "ports": [{"name": "Game", "container": 19132, "default": 19132, "proto": "udp"}],
     "folders": [{"key": "data", "label": "World folder", "to": "/data", "default": "{dir}/data"}],
     "fields": [{"key": "SERVER_NAME", "label": "Server name", "default": "My world"}],
     "env": {"EULA": "TRUE", "TZ": "{tz}"}, "extra": {"stdin_open": True, "tty": True},
     "eula": "https://minecraft.net/terms", "detect": ["itzg/minecraft-bedrock-server"],
     "next": "Add the server in Minecraft with this machine's address and port."},
]
BY_ID = {a["id"]: a for a in CATALOGUE}
CATEGORIES = ["Media", "Photos", "Home", "Downloads", "Files", "Network", "Tools", "Games"]

IMMICH_FILES = {"compose.yaml": "https://github.com/immich-app/immich/releases/latest/download/docker-compose.yml"}


# --------------------------------------------------------------------------- what's allowed
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/ +@-]{0,300}$")  # no quotes, $, ~, backslashes or line breaks
# folders an app may not be given: the system itself, Docker's own, and LabKeel's apps folder as a whole
SYSTEM_DIRS = ("/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64", "/proc", "/root", "/run", "/sbin",
               "/sys", "/usr", "/var", "/var/lib", "/var/lib/docker", "/var/run", "/srv", "/home", "/mnt", "/media", "/opt")


class Invalid(HTTPException):
    def __init__(self, msg):
        super().__init__(400, msg)


def why_not():
    """Why apps can't be added here, or None."""
    if not ALLOWED:
        return ("Adding apps is off. Add LABKEEL_ALLOW_APP_INSTALLS=1 to LabKeel's docker-compose.yml and run "
                "docker compose up -d to turn it on.")
    if not M().HOST_NS:
        return "LabKeel can't run commands on this machine (LABKEEL_HOST_COMMANDS is off), so it can't start apps."
    if not SAFE_PATH_RE.match(APPS_DIR) or _bad_dir(APPS_DIR, allow_parent=True):
        return f"LABKEEL_APPS_DIR ({APPS_DIR}) isn't a folder LabKeel will use. Give it one like /srv/labkeel/apps."
    if not _compose_ok():
        return "Docker Compose isn't installed on this machine (docker compose version didn't answer)."
    return None


_compose = {"ok": None, "t": 0.0}


def _compose_ok():
    """Whether `docker compose` works on the machine, asked at most every 10 minutes."""
    if _compose["ok"] is None or time.time() - _compose["t"] > 600:
        try:
            _compose.update(ok=selfupdate.host(["docker", "compose", "version"], 20).returncode == 0, t=time.time())
        except Exception:
            _compose.update(ok=False, t=time.time())
    return _compose["ok"]


def _bad_dir(p, allow_parent=False):
    """True for a folder an app mustn't be given (the system's own, or a whole top-level folder)."""
    p = "/" + p.strip("/")
    if ".." in p.split("/") or p in SYSTEM_DIRS:
        return True
    if not allow_parent and (p == APPS_DIR or p.count("/") < 2):
        return True  # /data, /tank: a whole drive or pool is too much to hand an app by default
    return any(p == d or p.startswith(d + "/") for d in ("/proc", "/sys", "/dev", "/boot", "/etc", "/usr", "/var/lib/docker"))


def _path(value, label, required=True):
    v = (value or "").strip()
    if not v:
        if required:
            raise Invalid(f"Choose a folder for \"{label}\".")
        return ""
    if not SAFE_PATH_RE.match(v):
        raise Invalid(f"\"{label}\" must be a folder on this machine starting with /, without quotes or $.")
    v = "/" + v.strip("/")
    if _bad_dir(v):
        raise Invalid(f"\"{label}\" can't be {v}: choose a folder of its own, like {APPS_DIR}/<app>/... or /tank/media.")
    return v


def _text(value, label, required=False, limit=200):
    v = str(value if value is not None else "").strip()
    if required and not v:
        raise Invalid(f"Fill in \"{label}\".")
    if len(v) > limit or any(ord(ch) < 32 for ch in v):
        raise Invalid(f"\"{label}\" is too long, or has a line break in it.")
    return v


def _port(value, label):
    try:
        p = int(value)
    except (TypeError, ValueError):
        raise Invalid(f"\"{label}\" must be a port number.") from None
    if not 1 <= p <= 65535:
        raise Invalid(f"\"{label}\" must be between 1 and 65535.")
    return p


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40].strip("-")
    return s


# --------------------------------------------------------------------------- writing compose files
def _q(s):
    """A string for a compose file: quoted, and with $ doubled so Compose doesn't read it as a variable."""
    return json.dumps(str(s).replace("$", "$$"), ensure_ascii=False)


def to_yaml(obj, indent=0):
    """A small YAML writer for compose files: mappings, lists, strings (always quoted), numbers and booleans."""
    pad = "  " * indent
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = k if re.fullmatch(r"[A-Za-z0-9_.-]+", k) else json.dumps(k)
            if isinstance(v, (dict, list)) and v:
                out.append(f"{pad}{key}:")
                out.append(to_yaml(v, indent + 1))
            else:
                out.append(f"{pad}{key}: {_scalar(v)}")
    else:
        for v in obj:
            if isinstance(v, dict) and v:
                inner = to_yaml(v, indent + 1).split("\n")
                out.append(f"{pad}- {inner[0].strip()}")
                out.extend(inner[1:])
            else:
                out.append(f"{pad}- {_scalar(v)}")
    return "\n".join(out)


def _scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (dict, list)):
        return "{}" if isinstance(v, dict) else "[]"
    return _q(v)


_owner_cache = {}


def _owner():
    """Who owns the apps folder (or the folder it's in) on the machine, so the apps' files are theirs. If root
    does, or it can't be read, 1000:1000 (the first user on most Linux machines)."""
    if "v" not in _owner_cache or time.time() - _owner_cache["t"] > 600:
        v = (1000, 1000)
        for p in (APPS_DIR, str(PurePosixPath(APPS_DIR).parent)):
            try:
                r = M().host_cmd(["stat", "-c", "%u %g", p], 5)
                uid, gid = (int(x) for x in r.stdout.split()) if r.returncode == 0 else (0, 0)
            except Exception:
                continue
            if uid:
                v = (uid, gid)
                break
        _owner_cache.update(v=v, t=time.time())
    return _owner_cache["v"]


def _tz():
    try:
        return U().tz().key or "Etc/UTC"
    except Exception:
        return "Etc/UTC"


def plan(app_id, values, adding=True):
    """Checks your choices for an app and works out everything about adding it. Raises Invalid with a message
    for the page. Returns {slug, dir, files: {name: text}, web: {port, path, scheme} or None, services}."""
    a = BY_ID.get(app_id) if isinstance(app_id, str) else None
    if not a:
        raise Invalid("There's no app with that name in the catalogue.")
    values = values if isinstance(values, dict) else {}
    name = _text(values.get("name") or a["name"], "Name", required=True, limit=60)
    slug = slugify(values.get("slug") or name)
    if not SLUG_RE.match(slug):
        raise Invalid("Give it a name with some letters or numbers in it.")
    if adding and a.get("eula") and not values.get("eula"):
        raise Invalid("Tick the box to accept the game's licence (EULA) first.")
    d = f"{APPS_DIR}/{slug}"
    uid, gid = _owner()
    sub = {"{dir}": d, "{tz}": _tz(), "{uid}": str(uid), "{gid}": str(gid)}

    def fill(s):
        for k, v in sub.items():
            s = s.replace(k, v)
        return s
    ports = []
    for i, p in enumerate(a["ports"]):
        if p.get("same_as") is not None:
            ports.append(ports[p["same_as"]])
            continue
        given = (values.get("ports") or {}).get(str(i)) if isinstance(values.get("ports"), dict) else None
        if p.get("fixed") or given in (None, ""):
            given = p["default"]
        ports.append(_port(given, p["name"]))
        sub[f"{{port{i}}}"] = str(ports[-1])
    folders = {}
    for f in a.get("folders", []):
        given = (values.get("folders") or {}).get(f["key"]) if isinstance(values.get("folders"), dict) else None
        default = fill(f["default"])
        folders[f["key"]] = _path(given if given is not None else default, f["label"], required=not f.get("optional"))
    envs = {k: fill(v) for k, v in (a.get("env") or {}).items()}
    for fld in a.get("fields", []):
        given = (values.get("fields") or {}).get(fld["key"]) if isinstance(values.get("fields"), dict) else None
        v = _text(given if given is not None else fld.get("default", ""), fld["label"], required=not fld.get("optional"))
        if v:
            envs[fld["key"]] = v
    web = None
    for i, p in enumerate(a["ports"]):
        if p.get("web"):
            web = {"port": ports[i], "path": a.get("web_path", ""), "scheme": a.get("web_scheme", "http")}
            break

    if a.get("upstream") == "immich":
        files = {".env": _immich_env(folders, ports[0], values)}
        return {"slug": slug, "name": name, "dir": d, "app": a["id"], "files": files, "web": web,
                "upstream": IMMICH_FILES, "ports": ports}

    main = {"image": a["image"], "container_name": slug, "restart": "unless-stopped"}
    if a.get("host_network"):
        main["network_mode"] = "host"
    else:
        main["ports"] = []
        for i, p in enumerate(a["ports"]):
            inside = ports[i] if p.get("follow") else p["container"]
            main["ports"].append(f"{ports[i]}:{inside}" + ("/udp" if p.get("proto") == "udp" else ""))
    if envs:
        main["environment"] = envs
    vols = [f"{folders[f['key']]}:{f['to']}" + (":ro" if f.get("ro") else "")
            for f in a.get("folders", []) if folders.get(f["key"])]
    extra = json.loads(json.dumps(a.get("extra") or {}))
    vols += extra.pop("volumes", [])
    if vols:
        main["volumes"] = vols
    main.update(extra)
    services = {}
    for k, v in (a.get("services") or {}).items():
        sv = json.loads(fill(json.dumps(v)))
        sv.setdefault("container_name", f"{slug}-{k}")
        services[k] = sv
    services = {"app": main, **services} if services else {"app": main}
    services = {(slug if k == "app" else k): v for k, v in services.items()}
    compose = {"name": slug, "services": services}
    head = (f"# {a['name']}, added from LabKeel's Apps page. You can change this file by hand and run\n"
            f"# docker compose up -d in this folder; LabKeel will keep showing it.\n")
    return {"slug": slug, "name": name, "dir": d, "app": a["id"], "files": {"compose.yaml": head + to_yaml(compose) + "\n"},
            "web": web, "ports": ports}


def _immich_env(folders, port, values):
    pw = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24))
    lines = ["# Immich, added from LabKeel's Apps page. See https://immich.app/docs/install/environment-variables",
             f'UPLOAD_LOCATION="{folders["UPLOAD_LOCATION"]}"', f'DB_DATA_LOCATION="{folders["DB_DATA_LOCATION"]}"',
             f'TZ="{_tz()}"', "IMMICH_VERSION=release", f"DB_PASSWORD={pw}", "DB_USERNAME=postgres",
             "DB_DATABASE_NAME=immich"]
    if port != 2283:
        lines.append(f"# LabKeel changed the port in compose.yaml to {port}")
    return "\n".join(lines) + "\n"


def custom_plan(values):
    """Your own compose file: checked by Docker Compose itself before anything starts."""
    values = values if isinstance(values, dict) else {}
    name = _text(values.get("name"), "Name", required=True, limit=60)
    slug = slugify(name)
    if not SLUG_RE.match(slug):
        raise Invalid("Give it a name with some letters or numbers in it.")
    text = values.get("compose")
    if not isinstance(text, str) or not text.strip():
        raise Invalid("Paste a compose file.")
    if len(text) > 100_000:
        raise Invalid("That compose file is too long.")
    web = None
    if values.get("web_port") not in (None, ""):
        web = {"port": _port(values.get("web_port"), "Web page port"), "path": "", "scheme": "http"}
    return {"slug": slug, "name": name, "dir": f"{APPS_DIR}/{slug}", "app": None,
            "files": {"compose.yaml": text if text.endswith("\n") else text + "\n"}, "web": web, "ports": []}


# --------------------------------------------------------------------------- what's been added
def _load():
    return U()._read("apps.json", {"apps": {}})


def _save(d):
    U()._write("apps.json", d)


def added():
    return _load().get("apps") or {}


_FOREIGN = "{dir} already has a compose file that LabKeel didn't write. Move it, or choose another name."


def _removed_here(p):
    """Whether this app was added here before and then removed (which keeps its folder), so adding it again may
    use that folder, settings and data included. A folder LabKeel didn't make is never taken over."""
    r = (_load().get("removed") or {}).get(p["slug"])
    return bool(r) and r.get("dir") == p["dir"]


def _folder_state(p):
    """For the dialog: None (a new folder), "again" (removed from here, and will be used again) or "foreign"."""
    if not M().HOST_NS or not _host_exists(f"{p['dir']}/compose.yaml"):
        return None
    return "again" if _removed_here(p) else "foreign"


def service_entries():
    """Apps added here, as entries for the Services page (beside the ones in config.json)."""
    out = []
    for slug, a in added().items():
        e = {"name": a.get("name") or slug, "group": "Apps", "container": a.get("container") or slug,
             "description": (BY_ID.get(a.get("app")) or {}).get("category"), "from_apps": True}
        w = a.get("web")
        if w:
            e["url"] = f"{w.get('scheme', 'http')}://{{host}}:{w['port']}{w.get('path', '')}"
            if w.get("scheme") == "https":
                e["check"] = {"type": "tcp", "host": "{host}", "port": w["port"]}
        out.append(e)
    return out


def _repo(ref):
    """'lscr.io/linuxserver/plex:latest' → 'lscr.io/linuxserver/plex'; Docker Hub's own names without docker.io."""
    ref = (ref or "").split("@")[0]
    if ":" in ref.rsplit("/", 1)[-1]:
        ref = ref.rsplit(":", 1)[0]
    for pre in ("docker.io/library/", "docker.io/", "index.docker.io/"):
        if ref.startswith(pre):
            return ref[len(pre):]
    return ref


def _running_images():
    """Image name (without its tag) → a container using it."""
    return {_repo(c.get("image")): c for c in containers.state.get("items") or []}


def catalogue():
    imgs = _running_images()
    mine = {a.get("app") for a in added().values()}
    out = []
    for a in CATALOGUE:
        found = next((imgs[i] for i in a.get("detect", []) if i in imgs), None)
        out.append({k: a.get(k) for k in ("id", "name", "category", "description", "next", "eula", "full_control")}
                   | {"image": a.get("image") or "Immich's own compose file (from its latest release)",
                      "ports": [{k: p.get(k) for k in ("name", "default", "proto", "web", "fixed")} | {"i": i}
                                for i, p in enumerate(a["ports"]) if p.get("same_as") is None],
                      "folders": [{k: f.get(k) for k in ("key", "label", "default", "ro", "optional")}
                                  for f in a.get("folders", [])],
                      "fields": [{k: f.get(k) for k in ("key", "label", "default", "secret", "optional", "help")}
                                 for f in a.get("fields", [])],
                      "host_network": bool(a.get("host_network")),
                      "added": a["id"] in mine, "running_as": found["name"] if found else None})
    return out


# --------------------------------------------------------------------------- doing it
# Files on the machine itself are read and written with commands run there (LabKeel's container can't see them).
def _host_exists(path):
    return M().host_cmd(["test", "-e", path], 10).returncode == 0


def _host_read(path):
    r = M().host_cmd(["cat", "--", path], 10)
    if r.returncode != 0:
        raise OSError((r.stderr or "").strip() or f"couldn't read {path}")
    return r.stdout


def _host_write(path, text, mode="644"):
    """Write a file on the machine: whole, then swapped in, so a half-written compose file never exists."""
    script = 'mkdir -p -- "$1" && cat > "$2.labkeel-new" && chmod "$3" "$2.labkeel-new" && mv -f -- "$2.labkeel-new" "$2"'
    r = selfupdate.host(["sh", "-c", script, "sh", str(PurePosixPath(path).parent), path, mode], 20, input=text)
    if r.returncode != 0:
        raise OSError(f"couldn't write {path}: {selfupdate._err(r)[0]}")


def _compose_cmd(p, *args):
    return ["docker", "compose", "-p", p["slug"], "--project-directory", p["dir"], "-f", f"{p['dir']}/compose.yaml", *args]


def _fetch_upstream(p):
    """Immich: its own compose file and settings, from its latest release, as its instructions say."""
    import httpx
    from version import USER_AGENT
    got = {}
    with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as http:
        for name, url in p["upstream"].items():
            r = http.get(url)
            r.raise_for_status()
            got[name] = r.text
    compose = got["compose.yaml"]
    port = p["ports"][0]
    if port != 2283:
        if "2283:2283" not in compose:
            raise RuntimeError("Immich's compose file has changed, so LabKeel can't move it to another port. Use 2283.")
        compose = compose.replace("2283:2283", f"{port}:2283")
    return {"compose.yaml": compose, ".env": p["files"][".env"]}


def _port_in_use(port, proto="tcp"):
    """Whether something on the machine already listens on that port (None if it can't tell)."""
    try:
        r = M().host_cmd(["ss", "-Hln" + ("u" if proto == "udp" else "t")], 10)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return any(re.search(rf"[:\]]{port}$", (ln.split() + ["", "", "", ""])[3]) for ln in r.stdout.splitlines())


def _containers_of(slug):
    try:
        cl = containers.dclient()
        return cl.containers.list(all=True, filters={"label": f"com.docker.compose.project={slug}"})
    except Exception:
        return []


def _answers(p, say):
    """Wait until the app is up: its containers running, and its web page answering."""
    end = time.time() + ANSWER_WAIT
    seen_running = False
    while time.time() < end:
        cs = _containers_of(p["slug"])
        if cs:
            states = [c.status for c in cs]
            if any(s in ("exited", "dead") for s in states):
                bad = next(c for c in cs if c.status in ("exited", "dead"))
                return f"{bad.name} stopped straight away. Its log (Containers → {bad.name}) says why."
            if all(s == "running" for s in states):
                if not seen_running:
                    seen_running = True
                    say("Its containers are running")
                if not p.get("web"):
                    return None
                w = p["web"]
                url = f"{w['scheme']}://127.0.0.1:{w['port']}{w['path']}"
                res = M().check_service({"name": p["name"], "url": url,
                                         "check": {"type": "tcp", "host": "127.0.0.1", "port": w["port"]}
                                         if w["scheme"] == "https" else {"type": "http", "url": url}})
                if res.get("state") in ("up", "degraded"):
                    say(f"It answers on port {w['port']}")
                    return None
        time.sleep(3)
    return (f"It hasn't answered on port {p['web']['port']} after {ANSWER_WAIT // 60} minutes. It may still be starting: "
            "check its log in Containers.") if p.get("web") else "Its containers didn't start."


def _run_add(p):
    job = _jobs[p["slug"]]
    t0 = time.time()

    def say(text):
        job["steps"].append({"t": time.time(), "after": round(time.time() - t0), "text": text})
    host = selfupdate.host
    try:
        compose = f"{p['dir']}/compose.yaml"
        again = _host_exists(compose)
        if again and not _removed_here(p):
            raise RuntimeError(_FOREIGN.format(dir=p["dir"]))
        files = _fetch_upstream(p) if p.get("upstream") else p["files"]
        if again:
            _host_write(compose + ".old", _host_read(compose))
        for name, text in files.items():
            if again and name != "compose.yaml" and _host_exists(f"{p['dir']}/{name}"):
                continue  # Immich's .env: its database already has that password
            _host_write(f"{p['dir']}/{name}", text, "600" if name == ".env" else "644")
        say(f"Wrote the compose file to {p['dir']}" + (
            " (it was here before: its settings and data are used again, and its last compose file is kept as "
            "compose.yaml.old)" if again else ""))
        if p["app"] is None:
            r = host(_compose_cmd(p, "config", "--format", "json"), 60)
            if r.returncode != 0:
                raise RuntimeError(f"Docker Compose doesn't accept that file: {selfupdate._err(r)[0]}")
            try:
                cfg = json.loads(r.stdout)
                p["container"] = next(iter((cfg.get("services") or {}).values()), {}).get("container_name") \
                    or f"{p['slug']}-{next(iter(cfg.get('services') or {'app': 0}))}-1"
            except ValueError:
                pass
        r = host(_compose_cmd(p, "pull"), PULL_TIMEOUT)
        if r.returncode != 0:
            raise RuntimeError(f"The download failed: {selfupdate._err(r)[0]}")
        say("Downloaded its images")
        r = host(_compose_cmd(p, "up", "-d"), UP_TIMEOUT)
        if r.returncode != 0:
            raise RuntimeError(f"Docker Compose couldn't start it: {selfupdate._err(r)[0]}")
        say("Started it")
        containers.refresh_soon()
        problem = _answers(p, say)
        if problem:
            raise RuntimeError(problem)
        with _lock:
            d = _load()
            d.setdefault("apps", {})[p["slug"]] = {
                "name": p["name"], "app": p["app"], "dir": p["dir"], "web": p.get("web"), "added": time.time(),
                "by": (job.get("by") or None), "container": p.get("container") or (
                    "immich_server" if p["app"] == "immich" else p["slug"])}
            (d.get("removed") or {}).pop(p["slug"], None)
            _save(d)
        main_services_changed()
        say("Added to Services, with update checks like your other containers")
        job.update(state="done")
        U().log("container", f"Added the app {p['name']} ({p['dir']})")
    except Exception as e:
        job.update(state="failed", detail=str(e))
        say(f"Stopped: {e}")
        U().log("error", f"Adding the app {p['name']} failed: {e}")
    finally:
        job["finished"] = time.time()
        containers.refresh_soon()


def main_services_changed():
    """The Services page reads apps afresh."""
    try:
        M()._svc_cache["t"] = 0
    except Exception:
        pass


def _run_remove(slug, a):
    job = _jobs[slug]
    p = {"slug": slug, "dir": a["dir"]}
    try:
        r = selfupdate.host(_compose_cmd(p, "down"), UP_TIMEOUT)
        if r.returncode != 0:
            raise RuntimeError(f"docker compose down failed: {selfupdate._err(r)[0]}")
        with _lock:
            d = _load()
            (d.get("apps") or {}).pop(slug, None)
            # its folder stays, so it can be added again over it (see _removed_here)
            d.setdefault("removed", {})[slug] = {"dir": a["dir"], "name": a.get("name"), "removed": time.time()}
            _save(d)
        main_services_changed()
        job.update(state="done")
        job["steps"].append({"t": time.time(), "text": f"Stopped and removed its containers. Its folder {a['dir']} "
                                                        "and everything in it are still there."})
        U().log("container", f"Removed the app {a.get('name') or slug} (its folder {a['dir']} was kept)")
    except Exception as e:
        job.update(state="failed", detail=str(e))
        job["steps"].append({"t": time.time(), "text": f"Stopped: {e}"})
        U().log("error", f"Removing the app {a.get('name') or slug} failed: {e}")
    finally:
        job["finished"] = time.time()
        containers.refresh_soon()


def _start(slug, target, *args, kind="add", name=None):
    sec = sys.modules["security"]
    with _lock:
        cur = _jobs.get(slug)
        if cur and cur["state"] == "running":
            raise HTTPException(409, "That app is already being changed. Wait for it to finish.")
        _jobs[slug] = {"state": "running", "kind": kind, "name": name or slug, "started": time.time(),
                       "by": (sec.ACTOR.get() or {}).get("user"), "steps": [], "detail": None, "finished": None}
    ctx = contextvars.copy_context()  # so the activity log says who did it
    threading.Thread(target=ctx.run, args=(target, *args), daemon=True, name=f"apps-{slug}").start()


# --------------------------------------------------------------------------- endpoints (admins: not in VIEWER_GET)
def suggest_folders():
    """Folders to offer for media and the like: the ones you've given LabKeel's file browser (their places on
    the machine), which are usually where your media lives."""
    out = set()
    try:
        me = containers._get(containers.SELF_ID) if containers.SELF_ID else None
        for m in (me.attrs.get("Mounts") or []) if me else []:
            if (m.get("Destination") or "").startswith("/browse/") and SAFE_PATH_RE.match(m.get("Source") or ""):
                out.add(m["Source"])
    except Exception:
        pass
    return sorted(out)


@router.get("/api/apps")
def api_apps():
    w = why_not()
    jobs = {k: v for k, v in _jobs.items() if v["state"] == "running" or time.time() - (v.get("finished") or 0) < 600}
    return {"catalogue": catalogue(), "categories": CATEGORIES, "added": added(), "jobs": jobs,
            "apps_dir": APPS_DIR, "available": w is None, "why_not": w, "suggest_folders": suggest_folders()}


@router.post("/api/apps/preview")
def api_preview(body: dict = Body(...)):
    """What adding an app would write, for the dialog: the compose file and its folder."""
    p = plan(body.get("app"), body.get("values"), adding=False)
    files = dict(p["files"])
    if p.get("upstream"):
        files["compose.yaml"] = ("# Immich's own compose file, downloaded from its latest release when you add it\n"
                                 "# (as Immich's instructions say), with its port set to " + str(p["ports"][0]) + ".\n")
    warn = []
    for i, port in enumerate(p.get("ports") or []):
        spec = BY_ID[p["app"]]["ports"][i]
        if spec.get("same_as") is None and _port_in_use(port, spec.get("proto", "tcp")):
            warn.append(f"Port {port} is already in use on this machine.")
    folder = _folder_state(p)
    if folder == "foreign":
        warn.insert(0, _FOREIGN.format(dir=p["dir"]))
    return {"slug": p["slug"], "dir": p["dir"], "files": files, "warnings": warn,
            "again": folder == "again"}


@router.post("/api/apps/add")
def api_add(request: Request, body: dict = Body(...)):
    """Add an app from the catalogue (admins, the password again). Runs in the background: the page follows
    GET /api/apps/job/<slug>."""
    w = why_not()
    if w:
        raise HTTPException(409, w)
    p = custom_plan(body.get("values")) if body.get("app") == "custom" else plan(body.get("app"), body.get("values"))
    if p["slug"] in added():
        raise HTTPException(409, f"There's already an app called {p['slug']}. Choose another name.")
    for i, port in enumerate(p.get("ports") or []):
        spec = BY_ID[p["app"]]["ports"][i]
        if spec.get("same_as") is None and _port_in_use(port, spec.get("proto", "tcp")):
            raise HTTPException(409, f"Port {port} is already in use on this machine. Choose another.")
    if _folder_state(p) == "foreign":
        raise HTTPException(409, _FOREIGN.format(dir=p["dir"]))
    sys.modules["security"].confirm_password(request, body.get("password"))
    U().log("container", f"Adding the app {p['name']} into {p['dir']}")
    _start(p["slug"], _run_add, p, name=p["name"])
    return {"ok": True, "slug": p["slug"]}


@router.get("/api/apps/job/{slug}")
def api_job(slug: str):
    return _jobs.get(slug) or {"state": None}


@router.get("/api/apps/{slug}/files")
def api_files(slug: str):
    """An added app's compose file, to read on the page (.env files can hold passwords: not shown)."""
    a = added().get(slug)
    if not a:
        raise HTTPException(404, "No app with that name")
    try:
        text = _host_read(f"{a['dir']}/compose.yaml")[:200_000]
    except OSError as e:
        text = f"# Couldn't read {a['dir']}/compose.yaml: {e}"
    return {"dir": a["dir"], "compose": text}


@router.post("/api/apps/{slug}/remove")
def api_remove(slug: str, request: Request, body: dict = Body(default={})):
    """Stop and remove an app's containers (docker compose down). Its folder and data are kept."""
    w = why_not()
    if w:
        raise HTTPException(409, w)
    a = added().get(slug)
    if not a:
        raise HTTPException(404, "No app with that name")
    sys.modules["security"].confirm_password(request, body.get("password"))
    U().log("container", f"Removing the app {a.get('name') or slug}")
    _start(slug, _run_remove, slug, a, kind="remove", name=a.get("name"))
    return {"ok": True}
