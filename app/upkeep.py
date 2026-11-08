"""
Long-term upkeep for the server: scheduled restarts, alerts, Docker clean-up and an activity log.
Everything is stored as JSON in the dashboard's data folder (LABKEEL_DATA_DIR, /data by default).
"""
import contextvars
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, time as dtime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psutil
from fastapi import APIRouter, Body, HTTPException, Query, Request

import containers
import db
import env
from version import USER_AGENT

router = APIRouter()
_lock = threading.RLock()     # state.json and settings.json; never held during network calls
ALLOW_REBOOT = env.flag("LABKEEL_ALLOW_RESTART")  # off unless turned on: restarting the machine is a big power to hand out
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Every setting and its default, by Settings section. settings.json only holds what you've changed;
# _clean() checks values from the page. Weekdays count from 0 = Monday; times are the server's local time.
DEFAULTS = {
    "restart": {"enabled": False, "frequency": "weekly", "weekday": 6, "time": "04:00",
                "only_if_updates": False, "wait_minecraft": False, "wait_plex": False,
                "wait_hours": 2, "if_still_busy": "skip"},
    "plex": {"url": "http://127.0.0.1:32400", "token": ""},
    "alerts": {"url": "", "drives": True, "pools": True, "disk_space": True, "disk_threshold": 90, "pool_space": True, "pool_threshold": 80,
               "containers": True, "services": True, "restarts": True, "slow": True, "slow_secs": 10, "updates": True, "servers": True,
               "quiet": False, "quiet_start": "23:00", "quiet_end": "07:00"},
    "cleanup": {"enabled": False, "weekday": 6, "time": "03:30"},
    "power": {"rest_w": 0, "price": 0, "currency": "£"},
    "display": {"refresh_ms": 2000, "tiles": None, "chart": "auto"},  # tiles: None = the default set
    "updates": {"enabled": True, "hours": 6},   # look for newer container images (see updates.py)
    "drivecare": {"reminder_hours": 12, "auto_replace": False, "hold_restarts": True, "pause_containers": [],
                  "nvme_hot": 70, "hdd_hot": 55, "wear_warn": 70, "wear_bad": 90, "scrub_days": 35,
                  "selftest": False, "selftest_weekday": 6, "selftest_time": "03:00", "selftest_long_monthly": True},
}
REFRESH_CHOICES = (300, 400, 500, 1000, 2000, 5000)  # the live refresh rates Settings → Display offers (ms)
UPDATE_HOURS = (1, 3, 6, 12, 24)
OVERVIEW_TILES = ("cpu", "mem", "gpu", "ctr", "net", "power", "storage")  # the tiles the Overview can show                     # how often Settings → Container updates can check


def M():
    """The main module (imported lazily to avoid a circular import)."""
    return sys.modules["main"]


# --------------------------------------------------------------------------- storage
def _path(name):
    """A file in the data folder."""
    return M().DATA_DIR / name


def _read(name, default):
    """A JSON file from the data folder, or `default` if it's missing. A damaged one is moved aside
    (name.damaged-<time>) rather than overwritten later."""
    p = _path(name)
    try:
        return json.loads(p.read_text())
    except OSError:
        return default
    except ValueError:
        # half-written or damaged: keep it to one side (for a look by hand) rather than overwrite it later
        try:
            aside = p.with_name(f"{p.name}.damaged-{int(time.time())}")
            p.replace(aside)
            print(f"{p} couldn't be read, so it was moved to {aside.name} and defaults are used", flush=True)
        except OSError:
            pass
        return default


def _write(name, obj):
    """Save a JSON file in the data folder safely: written in full to a temporary file, flushed to disk, then
    swapped in, so a crash or power cut leaves the old version or the new one, never half of one."""
    M().DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _path(f"{name}.{threading.get_ident()}.tmp")  # one temp file per thread: two writers never share one
    with open(tmp, "w") as f:
        f.write(json.dumps(obj, indent=2))
        f.flush()
        os.fsync(f.fileno())  # on disk before it takes the old file's place, so a power cut can't leave it empty
    tmp.replace(_path(name))
    try:
        fd = os.open(M().DATA_DIR, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass
    if name == "settings.json":
        _settings_cache["data"] = None


_settings_cache = {"t": 0.0, "mtime": None, "data": None}


def _settings_now():
    """The current settings, shared (don't change what this returns). Re-read when the file changes,
    checked at most once a second."""
    c = _settings_cache
    data = c["data"]
    if data is not None and time.time() - c["t"] < 1:
        return data
    try:
        mt = _path("settings.json").stat().st_mtime
    except OSError:
        mt = None
    if data is None or c["mtime"] != mt:
        saved = _read("settings.json", {})
        if not isinstance(saved, dict):
            saved = {}
        data = {k: {**v, **(saved.get(k) if isinstance(saved.get(k), dict) else {})} for k, v in DEFAULTS.items()}
        c.update(data=data, mtime=mt)
    c["t"] = time.time()
    return data


def load_settings():
    """A copy of all the settings, safe to change."""
    return json.loads(json.dumps(_settings_now()))


def setting(section, key, default=None):
    """One setting, without copying the rest: for things read many times a second."""
    return _settings_now().get(section, {}).get(key, default)


def log(kind, text):
    """Add a line to the activity log (Settings → Activity), in the database (see db.py). `kind` sets its colour
    on the page and says what it's about: alert, error, boot, slow, gap, settings, files, zfs, drive, container,
    restart, cleanup, update. Done during a request, it also records who did it and from where.

    Never raises: if the database can't be written, the line still reaches the container's log."""
    actor = sys.modules["security"].ACTOR.get() if "security" in sys.modules else None
    who = (actor or {}).get("user")
    try:
        db.add_event(kind, text, who, (actor or {}).get("ip"))
    except Exception as e:
        print(f"[error] Couldn't write the activity log: {type(e).__name__}: {e}", flush=True)
    print(f"[{kind}] {text}" + (f" ({who})" if who else ""), flush=True)


# --------------------------------------------------------------------------- time
_tz = None


def tz():
    """The server's own time zone, so schedules match the clock on the wall."""
    global _tz
    if _tz:
        return _tz
    name = None
    try:
        link = M().host_cmd(["readlink", "-f", "/etc/localtime"], timeout=5).stdout.strip()
        if "zoneinfo/" in link:
            name = link.split("zoneinfo/", 1)[1]
    except Exception:
        pass
    if not name:
        try:
            name = M().host_cmd(["cat", "/etc/timezone"], timeout=5).stdout.strip() or None
        except Exception:
            pass
    try:
        _tz = ZoneInfo(name or os.environ.get("TZ") or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        _tz = ZoneInfo("UTC")
    return _tz


def next_slot(freq, weekday, hhmm, after):
    """Next run strictly after `after`. monthly = the first <weekday> of each month."""
    h, m = (int(x) for x in hhmm.split(":"))
    z = tz()
    a = after.astimezone(z)
    for add in range(0, 45):
        d = (a + timedelta(days=add)).date()
        if freq == "weekly" and d.weekday() != weekday:
            continue
        if freq == "monthly" and (d.weekday() != weekday or d.day > 7):
            continue
        cand = datetime.combine(d, dtime(h, m), tzinfo=z)
        # compare real moments: on the night the clocks go back, 01:30 happens twice and a plain
        # comparison would treat the first one as still to come during the second
        if cand.timestamp() > a.timestamp():
            return cand
    return None


def _fmt(dt):
    """A time as 'Sun 3 Nov, 04:00'."""
    return dt.strftime("%a %-d %b, %H:%M")


# --------------------------------------------------------------------------- host state
_reboot = {"t": 0.0, "v": (False, [])}


def reboot_required():
    """(True, [package names]) when Ubuntu has installed updates that need a restart. Checked on the host
    at most every 5 minutes: Settings and the Overview both ask for it on every refresh."""
    if time.time() - _reboot["t"] < 300:
        return _reboot["v"]
    try:
        if M().host_cmd(["test", "-e", "/var/run/reboot-required"], timeout=5).returncode != 0:
            v = (False, [])
        else:
            pkgs = M().host_cmd(["cat", "/var/run/reboot-required.pkgs"], timeout=5).stdout.split()
            v = (True, sorted(set(pkgs)))
    except Exception:
        v = (False, [])
    _reboot.update(t=time.time(), v=v)
    return v


def plex_streams():
    """Number of active Plex streams, or None if it can't be checked."""
    p = load_settings()["plex"]
    if not p["token"]:
        return None
    if not p["url"].startswith(("http://", "https://")):
        raise ValueError("the Plex address must start with http:// or https://")
    req = urllib.request.Request(p["url"].rstrip("/") + "/status/sessions",
                                 headers={"X-Plex-Token": p["token"], "Accept": "application/json"})
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    # a redirect would carry the token along to wherever it points, so redirects aren't followed
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), _NoRedirect())
    with opener.open(req, timeout=5) as r:
        return int((json.load(r).get("MediaContainer") or {}).get("size") or 0)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses redirects (used for Plex, so its token is never carried off somewhere else)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"Plex answered with a redirect to {newurl[:80]}, which isn't followed",
                                     headers, fp)


def minecraft_players(fresh=True):
    """Players on all the Minecraft servers together (from the service checks)."""
    total = 0
    for item in M().api_services(fresh=fresh)["items"]:
        total += ((item.get("status") or {}).get("info") or {}).get("players") or 0
    return total


def who_is_busy(r, notes=None, fresh=True):
    """Reasons the server is in use right now, per the restart settings."""
    reasons = []
    if r["wait_minecraft"]:
        n = minecraft_players(fresh)
        if n:
            reasons.append(f"{n} player{'s' if n != 1 else ''} on Minecraft")
    if r["wait_plex"]:
        try:
            n = plex_streams()
            if n:
                reasons.append(f"{n} Plex stream{'s' if n != 1 else ''}")
        except Exception as e:
            if notes is not None:
                why = getattr(e, "reason", e)
                notes.append("Plex refused the saved token, so LabKeel can't see who's watching. Paste a new one in "
                             "Settings → Restarts and power, or untick waiting for Plex." if getattr(e, "code", None) in (401, 403)
                             else f"Couldn't check Plex: {why}")
    if load_settings()["drivecare"].get("hold_restarts", True):
        try:
            import drivecare
            reasons += drivecare.at_risk()
        except Exception:
            pass
    return reasons


# --------------------------------------------------------------------------- notifications
def _mins(hhmm):
    """'07:30' as minutes after midnight."""
    h, m = (int(x) for x in hhmm.split(":"))
    return h * 60 + m


def quiet_now(a=None, now=None):
    """Whether it's quiet hours right now (Settings → Alerts)."""
    a = a or load_settings()["alerts"]
    if not a.get("quiet"):
        return False
    now = now or datetime.now(tz())
    cur, start, end = now.hour * 60 + now.minute, _mins(a["quiet_start"]), _mins(a["quiet_end"])
    if start == end:
        return False
    return start <= cur < end if start < end else (cur >= start or cur < end)


def notify(title, message, tag="information_source", urgent=False, force=False):
    """Send to ntfy (or anything that takes a plain-text POST) or a Discord webhook.
    During quiet hours, anything that isn't urgent waits and goes out as one summary afterwards.
    Returns (ok, error)."""
    a = load_settings()["alerts"]
    if not force and not urgent and a["url"] and quiet_now(a):
        with _lock:
            st = _read("state.json", {})
            held = st.setdefault("held_alerts", [])
            held.append({"t": time.time(), "title": title, "message": message})
            del held[:-50]
            _write("state.json", st)
        return True, None
    return _send(title, message, tag, urgent)


def send_held():
    """After quiet hours: one message with whatever waited."""
    with _lock:
        st = _read("state.json", {})
        held = st.pop("held_alerts", None)
        if held:
            _write("state.json", st)
    if not held:
        return
    z = tz()
    lines = [f"{datetime.fromtimestamp(h['t'], z):%H:%M} {h['title']}: {h['message']}" for h in held]
    text = "\n".join(lines)
    _send(f"{len(held)} alert{'s' if len(held) != 1 else ''} from quiet hours", text if len(text) < 1800 else text[:1790] + "…",
          "bell", False)


def _send(title, message, tag, urgent):
    """Send one alert now. A Discord webhook gets Discord's format; anything else (ntfy, say) a plain-text POST
    with a title, tags and priority. Returns (ok, error)."""
    url = load_settings()["alerts"]["url"]
    if not url:
        return False, "No alert address set"
    if not url.startswith(("http://", "https://")):
        return False, "The alert address must start with https:// (or http://)"
    host = M().HOST_INFO.get("hostname") or "server"
    try:
        if "/api/webhooks/" in url and ("discord.com" in url or "discordapp.com" in url):
            data = json.dumps({"username": host, "content": f"**{title}**\n{message}"[:1900]}).encode()
            headers = {"Content-Type": "application/json"}
        else:
            ascii_title = title.encode("ascii", "ignore").decode().strip() or host
            data = message.encode()
            headers = {"Title": ascii_title, "Tags": tag,
                       "Priority": "urgent" if urgent and tag == "rotating_light" else "high" if urgent or tag in ("warning", "rotating_light") else "default"}
        req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **headers}, method="POST")
        with urllib.request.urlopen(req, timeout=10):  # http(s) only, checked above  # nosec B310
            pass
        return True, None
    except Exception as e:
        err = str(getattr(e, "reason", None) or e)[:160]
        log("error", f"Couldn't send alert \"{title}\": {err}")
        return False, err


# --------------------------------------------------------------------------- reboot
def do_reboot(reason, shutdown=False):
    """Restart (or shut down) the machine: logs it, sends an alert, notes why so the 'back online' message can
    say, then asks systemd. Nothing happens unless LABKEEL_ALLOW_RESTART=1."""
    host = M().HOST_INFO.get("hostname")
    log("restart", f"{'Shutting down' if shutdown else 'Restarting'} the server: {reason}")
    if load_settings()["alerts"]["restarts"]:
        if shutdown:
            notify(f"Shutting down {host}", f"Reason: {reason}. It stays off until someone presses the power button.", "stop_button", force=True)
        else:
            notify(f"Restarting {host}", f"Reason: {reason}. Back in a few minutes.", "arrows_counterclockwise", force=True)
    with _lock:
        st = _read("state.json", {})
        st["restart_pending"] = {"t": time.time(), "reason": reason}
        _write("state.json", st)
    if not ALLOW_REBOOT:
        log("error", f"{'Shut-down' if shutdown else 'Restart'} not carried out: LABKEEL_ALLOW_RESTART=1 isn't set")
        return
    time.sleep(5)  # let the log write and the alert leave
    cmds = (["systemctl", "poweroff"], ["poweroff"]) if shutdown else (["systemctl", "reboot"], ["reboot"])
    for cmd in cmds:
        try:
            r = M().host_cmd(cmd, timeout=30)
            if r.returncode == 0:
                return
            err = (r.stderr or r.stdout).strip()
        except Exception as e:
            err = str(e)
    log("error", f"Restart failed: {err}")
    notify("Restart failed", err, "warning")


# --------------------------------------------------------------------------- scheduler
def _restart_tick(s, st, now):
    """Returns a reason string when it's time to reboot."""
    r = s["restart"]
    if not r["enabled"]:
        for k in ("restart_next", "restart_key", "restart_waiting"):
            st.pop(k, None)
        return None
    key = json.dumps([r["frequency"], r["weekday"], r["time"]])
    if st.get("restart_key") != key or not st.get("restart_next"):
        st["restart_key"] = key
        st["restart_next"] = next_slot(r["frequency"], r["weekday"], r["time"], now).isoformat()
        st.pop("restart_waiting", None)
    due = datetime.fromisoformat(st["restart_next"])
    if now < due:
        return None

    def advance():
        st["restart_next"] = next_slot(r["frequency"], r["weekday"], r["time"], now).isoformat()
        st.pop("restart_waiting", None)

    wait = timedelta(hours=r["wait_hours"])
    if now > due + wait + timedelta(minutes=15):
        log("restart", f"Skipped the {_fmt(due)} restart: the dashboard wasn't running at the time")
        advance()
        return None
    if time.time() - psutil.boot_time() < 20 * 60:
        log("restart", "Skipped scheduled restart: the server started less than 20 minutes ago")
        advance()
        return None
    if r["only_if_updates"] and not reboot_required()[0]:
        log("restart", "Skipped scheduled restart: no updates are waiting for one")
        advance()
        return None
    busy = who_is_busy(r)
    if busy:
        if now < due + wait:
            if st.get("restart_waiting") != st["restart_next"]:
                st["restart_waiting"] = st["restart_next"]
                hrs = f"{r['wait_hours']:g} hour{'s' if r['wait_hours'] != 1 else ''}"
                log("restart", f"Holding the scheduled restart ({', '.join(busy)}). Will keep checking for up to {hrs}.")
            return None
        if r["if_still_busy"] != "restart":
            log("restart", f"Skipped scheduled restart: still in use ({', '.join(busy)})")
            if s["alerts"]["restarts"]:
                notify("Scheduled restart skipped", f"Still in use: {', '.join(busy)}. Next try {_fmt(next_slot(r['frequency'], r['weekday'], r['time'], now))}.")
            advance()
            return None
    advance()
    return "scheduled restart" + (f", even though it was still in use ({', '.join(busy)})" if busy else "")


def _cleanup_tick(s, st, now):
    """Whether the weekly Docker clean-up is due now (it's skipped if more than 6 hours late)."""
    c = s["cleanup"]
    if not c["enabled"]:
        st.pop("cleanup_next", None)
        st.pop("cleanup_key", None)
        return False
    key = json.dumps([c["weekday"], c["time"]])
    if st.get("cleanup_key") != key or not st.get("cleanup_next"):
        st["cleanup_key"] = key
        st["cleanup_next"] = next_slot("weekly", c["weekday"], c["time"], now).isoformat()
    due = datetime.fromisoformat(st["cleanup_next"])
    if now < due:
        return False
    st["cleanup_next"] = next_slot("weekly", c["weekday"], c["time"], now).isoformat()
    return now < due + timedelta(hours=6)  # run if not hopelessly late


def run_cleanup(why):
    """Remove Docker images no container uses, and the build cache. Returns what was freed."""
    cl = containers.dclient()
    imgs = cl.images.prune(filters={"dangling": False}) or {}
    try:
        builds = cl.api.prune_builds() or {}
    except Exception:
        builds = {}
    freed = (imgs.get("SpaceReclaimed") or 0) + (builds.get("SpaceReclaimed") or 0)
    removed = sum(1 for x in (imgs.get("ImagesDeleted") or []) if x.get("Untagged"))
    gb = freed / 1e9
    text = f"Docker clean-up ({why}): removed {removed} unused image{'s' if removed != 1 else ''}, freed " + \
           (f"{gb:.1f} GB" if gb >= 0.1 else f"{freed / 1e6:.0f} MB")
    log("cleanup", text)
    with _lock:
        st = _read("state.json", {})
        st["cleanup_last"] = {"t": time.time(), "freed": freed, "removed": removed}
        _write("state.json", st)
    return {"freed": freed, "removed": removed, "text": text}


def _boot_check():
    """At start-up: if the machine itself restarted since last time, log it and say why (a restart from here, or
    not: maybe a power cut)."""
    bt = psutil.boot_time()
    with _lock:
        st = _read("state.json", {})
        last = st.get("last_boot")
        st["last_boot"] = bt
        pending = st.pop("restart_pending", None)
        _write("state.json", st)
    if last is None or abs(bt - last) < 60:
        return
    host = M().HOST_INFO.get("hostname") or "The server"
    if pending and bt > pending["t"] - 120:
        msg = f"{host} is back online after a {pending['reason'].split(',')[0]}"
    else:
        msg = f"{host} started up. It wasn't restarted from the dashboard, so it may have lost power or been restarted by hand."
    log("boot", msg)
    if load_settings()["alerts"]["restarts"]:
        notify(f"{host} is back online", msg, "white_check_mark")


# --------------------------------------------------------------------------- alerts
def mark_stopped_on_purpose(name, stopped):
    """Containers you stop from the dashboard shouldn't raise 'crashed' or 'service down' alerts."""
    with _lock:
        st = _read("state.json", {})
        s = set(st.get("stopped_on_purpose") or [])
        (s.add if stopped else s.discard)(name)
        st["stopped_on_purpose"] = sorted(s)
        _write("state.json", st)



ALERT_KINDS = {"drive": "drives", "pool": "pools", "disk": "disk_space", "container": "containers", "service": "services",
               "poolspace": "pool_space", "quota": "disk_space", "server": "servers"}


def _conditions(a, active=frozenset(), covered=True):
    """Every problem worth an alert right now, as {key: (level, title, detail)}. Keys look like 'drive:<serial>',
    'pool:<name>', 'disk:<mount>', 'poolspace:<pool>', 'quota:<dataset>', 'container:<name>',
    'service:<name>'. `active` are those already alerted: a full disk has to drop 2 points further before it
    counts as fixed, so it doesn't flap. With `covered`, drives and pools drive care is already following are
    left to it."""
    m = M()
    names = m.load_overrides()
    out = {}
    case_serials, case_pools = set(), set()
    if covered:
        try:
            import drivecare
            for c in drivecare.open_cases():
                case_serials.add(c.get("serial"))
                case_pools.add(c.get("pool"))
        except Exception:
            pass
    on_purpose = set(_read("state.json", {}).get("stopped_on_purpose") or [])
    # forget containers that are running again (started from the command line, for example)
    running = {c["name"] for c in containers.state["items"] if c["state"] == "running"}
    if on_purpose & running:
        with _lock:
            st = _read("state.json", {})
            st["stopped_on_purpose"] = sorted(set(st.get("stopped_on_purpose") or []) - running)
            _write("state.json", st)
        on_purpose -= running
    if a["drives"]:
        for d in m._drives["items"]:
            if d.get("status") in ("warn", "bad") and not (d.get("serial") and d.get("serial") in case_serials):
                key = d.get("serial") or d.get("device")
                nm = names.get("drives", {}).get(key or "") or d.get("label") or \
                    (d.get("block_path") or d.get("device") or "").replace("/dev/", "")
                out[f"drive:{key}"] = (d["status"], f"Drive {nm} needs attention", "; ".join(d.get("issues") or []) or "SMART warning")
    if a["pools"] or a["disk_space"] or a.get("pool_space"):
        stg = m.api_storage()
        if a["pools"]:
            for p in stg["pools"]:
                if p["health"] != "ONLINE" and p["name"] not in case_pools:
                    out[f"pool:{p['name']}"] = ("bad", f"ZFS pool {p['name']} is {p['health']}", "Open the Storage page for details.")
                elif p.get("errors") and p["errors"] != "No known data errors":
                    out[f"pool:{p['name']}:data"] = ("bad", f"ZFS pool {p['name']} has damaged files",
                                                     f"{p['errors']}. Restore them from a backup, then run a scrub.")
        if a["disk_space"]:
            for f in stg["filesystems"]:
                if f["type"] == "zfs":
                    continue  # a dataset is as full as its pool (or its quota): both are checked below
                # once alerted, it stays open until it's 2 points below the limit (no flapping at 90.0 / 89.9)
                limit = a["disk_threshold"] - (2 if f"disk:{f['mount']}" in active else 0)
                if f["percent"] >= limit:
                    out[f"disk:{f['mount']}"] = ("warn", f"{f['mount']} is {round(f['percent'])}% full",
                                                 f"{f['free'] / 1e9:.0f} GB left.")
        if a.get("pool_space"):
            for p in stg["pools"]:
                pct = p.get("percent") if p.get("percent") is not None else p.get("cap")
                limit = a["pool_threshold"] - (2 if f"poolspace:{p['name']}" in active else 0)
                if pct is not None and pct >= limit:
                    free = p.get("avail") if p.get("avail") is not None else p.get("free") or 0
                    out[f"poolspace:{p['name']}"] = ("warn", f"ZFS pool {p['name']} is {round(pct)}% full",
                                                     f"{free / 1e9:.0f} GB left. ZFS slows down as a pool fills up, so keep it under about 80%.")
        if a["disk_space"]:
            try:
                import zfsmgr
                for p in stg["pools"]:
                    for d in zfsmgr.datasets(p["name"]):
                        q = d.get("quota") or 0
                        if not q or d.get("used") is None:
                            continue
                        limit = a["disk_threshold"] - (2 if f"quota:{d['name']}" in active else 0)
                        pct = d["used"] / q * 100
                        if pct >= limit:
                            out[f"quota:{d['name']}"] = ("warn", f"{d['name']} is {round(pct)}% of its size limit",
                                                         f"{max(0, q - d['used']) / 1e9:.1f} GB left of {q / 1e9:.0f} GB. Raise the limit on the pool's Datasets tab.")
            except Exception:
                pass
    if a["containers"]:
        for c in containers.state["items"]:
            if c["name"] in on_purpose:
                continue
            crashed = c["state"] == "exited" and c.get("exit_code") not in (0, 143)
            if crashed or c["state"] == "restarting" or c.get("health") == "unhealthy":
                nm = names.get("containers", {}).get(c["name"]) or c["name"]
                what = "keeps restarting" if c["state"] == "restarting" else "is unhealthy" if c.get("health") == "unhealthy" else f"stopped unexpectedly (exit code {c.get('exit_code')})"
                out[f"container:{c['name']}"] = ("bad", f"{nm} {what}", "Open Containers in the dashboard to read its logs.")
    if a["services"]:
        for i in m.api_services()["items"]:
            linked = m.find_container(i["container"]) if i.get("container") else None
            if linked and linked["name"] in on_purpose:
                continue
            if i["status"]["state"] == "down":
                out[f"service:{i['name']}"] = ("bad", f"{i.get('display') or i['name']} is down", i["status"].get("detail") or "")
    if a.get("servers") and "servers" in sys.modules:  # other machines, through their agents
        out.update(sys.modules["servers"].conditions(a))
    return out


NEED = {"drive": 1, "pool": 1, "disk": 2, "container": 2, "service": 3, "poolspace": 2, "quota": 2, "server": 2}  # minutes a problem lasts before it's sent
CLEAR_AFTER = 3        # minutes a problem has to stay gone before "back to normal"
COOLDOWN = 30 * 60     # if it comes back within this long of clearing...
FLAP_NEED = 10         # ...it has to last this many minutes before alerting again


def _alerts_tick(s):
    """Once a minute: compares the problems now with last time and sends alerts. A problem has to last a few
    minutes before it's sent (NEED), and stay gone for CLEAR_AFTER minutes before 'back to normal'; one that
    keeps coming back has to last longer (FLAP_NEED)."""
    a = s["alerts"]
    active = frozenset(k for k, v in (_read("state.json", {}).get("alerts") or {}).items() if v.get("sent"))
    conds = _conditions(a, active)
    enabled = {k for k, setting in ALERT_KINDS.items() if a[setting]}
    sends, recovers = [], []
    now = time.time()
    with _lock:
        st = _read("state.json", {})
        prev = st.get("alerts")
        first = prev is None
        prev = prev or {}
        cleared = {k: t for k, t in (st.get("alerts_cleared") or {}).items() if now - t < COOLDOWN}
        new = {}
        for k, (lvl, title, detail) in conds.items():
            p = prev.get(k) or {}
            entry = {"level": lvl, "title": title, "count": p.get("count", 0) + 1, "gone": 0,
                     "sent": p.get("sent", False) or first, "since": p.get("since", now)}
            need = NEED.get(k.split(":")[0], 1)
            if k in cleared:
                need = max(need, FLAP_NEED)
            if not entry["sent"] and entry["count"] >= need:
                entry["sent"] = True
                sends.append((title, detail, k.startswith("pool:") or (k.startswith("drive:") and lvl == "bad")))
            new[k] = entry
        for k, p in prev.items():
            if k in new or not p.get("sent") or k.split(":")[0] not in enabled:
                continue  # still a problem, never alerted, or that kind of alert was turned off
            gone = p.get("gone", 0) + 1
            if gone >= CLEAR_AFTER:
                recovers.append(p["title"])
                cleared[k] = now
            else:
                new[k] = {**p, "gone": gone}  # still counts as open until it has stayed fine for a few minutes
        if st.get("alerts") != new or (st.get("alerts_cleared") or {}) != cleared:  # only when something changed
            st["alerts"] = new
            st["alerts_cleared"] = cleared
            _write("state.json", st)
    for title, detail, urgent in sends:
        log("alert", f"{title}. {detail}".strip())
        notify(title, detail or title, "rotating_light" if urgent else "warning", urgent=urgent)
    for title in recovers:
        log("alert", f"Resolved: {title}")
        notify("Back to normal", f"Resolved: {title}", "white_check_mark")


SCHEDULER_KEYS = ("restart_next", "restart_key", "restart_waiting", "cleanup_next", "cleanup_key")


def loop():
    """Background thread: every 30 s, the restart and clean-up schedules; every minute, alerts."""
    time.sleep(45)  # let the network and the other pollers come up first
    try:
        _boot_check()
    except Exception as e:
        print("boot check error:", e, flush=True)
    last_alerts = 0
    while True:
        try:
            s = load_settings()
            now = datetime.now(tz())
            with _lock:
                st = _read("state.json", {})
            # these can wait on Plex, Minecraft or an alert going out, so they run without holding the lock
            reboot_reason = _restart_tick(s, st, now)
            cleanup_due = _cleanup_tick(s, st, now)
            with _lock:  # write back only the keys the schedulers own, on top of whatever changed meanwhile
                cur = _read("state.json", {})
                if any(cur.get(k) != st.get(k) for k in SCHEDULER_KEYS):  # most of the time nothing has
                    for k in SCHEDULER_KEYS:
                        if k in st:
                            cur[k] = st[k]
                        else:
                            cur.pop(k, None)
                    _write("state.json", cur)
            if cleanup_due:
                try:
                    run_cleanup("weekly")
                except Exception as e:
                    log("error", f"Weekly Docker clean-up failed: {e}")
            if time.time() - last_alerts >= 60:
                last_alerts = time.time()
                _alerts_tick(s)
                if not quiet_now(s["alerts"]):
                    send_held()
            if reboot_reason:
                do_reboot(reboot_reason)
        except Exception as e:
            print("upkeep loop error:", e, flush=True)
        time.sleep(30)


def start():
    """Starts the upkeep thread."""
    threading.Thread(target=loop, daemon=True, name="upkeep").start()


# --------------------------------------------------------------------------- API
def _hhmm(v, fallback):
    """'7:5' as '07:05'; anything that isn't a time of day gives `fallback`."""
    try:
        h, m = (int(x) for x in str(v).split(":")[:2])
        if 0 <= h < 24 and 0 <= m < 60:
            return f"{h:02d}:{m:02d}"
    except ValueError:
        pass
    return fallback


def _origin(url):
    """(scheme, host, port) of an address: the saved Plex token is only kept for the same one."""
    u = urlsplit(url)
    return u.scheme.lower(), (u.hostname or "").lower(), u.port


def _clean(body, cur):
    """Settings from the page, checked and kept within sensible limits, on top of the current ones. Raises 400
    for an address that isn't http(s)://."""
    out = {}
    r = {**cur["restart"], **(body.get("restart") or {})}
    out["restart"] = {
        "enabled": bool(r["enabled"]),
        "frequency": r["frequency"] if r["frequency"] in ("daily", "weekly", "monthly") else "weekly",
        "weekday": int(r["weekday"]) % 7,
        "time": _hhmm(r["time"], cur["restart"]["time"]),
        "only_if_updates": bool(r["only_if_updates"]),
        "wait_minecraft": bool(r["wait_minecraft"]),
        "wait_plex": bool(r["wait_plex"]),
        "wait_hours": max(0.0, min(12.0, float(r["wait_hours"]))),
        "if_still_busy": "restart" if r["if_still_busy"] == "restart" else "skip",
    }
    p = body.get("plex") or {}
    plex_url = str(p.get("url") or cur["plex"]["url"]).strip().rstrip("/")
    if not plex_url.startswith(("http://", "https://")) or len(plex_url) > 2048:
        raise HTTPException(400, "The Plex address needs to start with http:// or https://")
    new_token = str(p.get("token") or "").strip()[:512]
    if new_token and not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", new_token):
        # Plex tokens are letters, digits, - and _. Anything else is usually the browser filling in a saved
        # password here by mistake, which Plex would then refuse ("Unauthorized").
        raise HTTPException(400, "That doesn't look like a Plex token (they're letters, numbers, - and _ only). "
                                 "If your browser filled it in, clear the box and paste the token from Plex.")
    if p.get("clear_token"):
        token = ""
    elif new_token:
        token = new_token
    elif _origin(plex_url) != _origin(cur["plex"]["url"]):
        token = ""  # never send the saved token to a different server (or the same one without encryption)
    else:
        token = cur["plex"]["token"]
    out["plex"] = {"url": plex_url, "token": token}
    a = {**cur["alerts"], **(body.get("alerts") or {})}
    url = str(a["url"] or "").strip()
    if url and (not url.startswith(("http://", "https://")) or len(url) > 2048):
        raise HTTPException(400, "The alert address needs to start with https:// (or http://)")
    out["alerts"] = {"url": url, "disk_threshold": max(50, min(99, int(a["disk_threshold"]))),
                     "pool_threshold": max(50, min(99, int(a["pool_threshold"]))),
                     **{k: bool(a[k]) for k in ALERT_SWITCHES + ("quiet",)},
                     "slow_secs": max(5, min(300, int(a["slow_secs"]))),
                     "quiet_start": _hhmm(a["quiet_start"], cur["alerts"]["quiet_start"]),
                     "quiet_end": _hhmm(a["quiet_end"], cur["alerts"]["quiet_end"])}
    pw = {**cur["power"], **(body.get("power") or {})}
    out["power"] = {"rest_w": max(0.0, min(2000.0, float(pw["rest_w"] or 0))),
                    "price": max(0.0, min(10.0, float(pw["price"] or 0))),
                    "currency": pw["currency"] if pw["currency"] in ("£", "€", "$", "") else "£"}
    dp = {**cur["display"], **(body.get("display") or {})}
    tiles = dp.get("tiles")
    if tiles is not None:
        if not isinstance(tiles, list) or not tiles or len(tiles) > 6 or len(set(tiles)) != len(tiles) \
                or any(t not in OVERVIEW_TILES for t in tiles):
            raise HTTPException(400, f"Overview tiles: pick 1 to 6 of {', '.join(OVERVIEW_TILES)}")
    out["display"] = {"refresh_ms": int(dp["refresh_ms"]) if int(dp["refresh_ms"]) in REFRESH_CHOICES else 2000,
                      "tiles": tiles, "chart": dp.get("chart") if dp.get("chart") in ("auto", "gpu", "cpumem") else "auto"}
    up = {**cur["updates"], **(body.get("updates") or {})}
    out["updates"] = {"enabled": bool(up["enabled"]), "hours": int(up["hours"]) if int(up["hours"]) in UPDATE_HOURS else 6}
    dc = {**cur["drivecare"], **(body.get("drivecare") or {})}
    pause = dc["pause_containers"] if isinstance(dc["pause_containers"], list) else []
    out["drivecare"] = {
        "reminder_hours": int(dc["reminder_hours"]) if int(dc["reminder_hours"]) in (0, 6, 12, 24, 48) else 12,
        "auto_replace": bool(dc["auto_replace"]), "hold_restarts": bool(dc["hold_restarts"]),
        "pause_containers": sorted({str(x)[:128] for x in pause if str(x).strip()})[:50],
        "nvme_hot": max(40, min(90, int(dc["nvme_hot"]))), "hdd_hot": max(35, min(70, int(dc["hdd_hot"]))),
        "wear_warn": max(10, min(99, int(dc["wear_warn"]))), "wear_bad": max(10, min(100, int(dc["wear_bad"]))),
        "scrub_days": max(0, min(365, int(dc["scrub_days"]))),
        "selftest": bool(dc["selftest"]), "selftest_weekday": int(dc["selftest_weekday"]) % 7,
        "selftest_time": _hhmm(dc["selftest_time"], cur["drivecare"]["selftest_time"]),
        "selftest_long_monthly": bool(dc["selftest_long_monthly"]),
    }
    if out["drivecare"]["wear_bad"] < out["drivecare"]["wear_warn"]:
        out["drivecare"]["wear_bad"] = out["drivecare"]["wear_warn"]
    c = {**cur["cleanup"], **(body.get("cleanup") or {})}
    out["cleanup"] = {"enabled": bool(c["enabled"]), "weekday": int(c["weekday"]) % 7,
                      "time": _hhmm(c["time"], cur["cleanup"]["time"])}
    return out


def _public(s):
    """Settings as the page sees them: the Plex token itself is never sent back, only whether there is one."""
    s = json.loads(json.dumps(s))
    s["plex"]["token_set"] = bool(s["plex"].pop("token"))
    return s


def _status(s):
    """For Settings: the time zone, next restart and clean-up, what's keeping the server busy, waiting updates,
    open alerts and quiet hours."""
    z = tz()
    now = datetime.now(z)
    st = _read("state.json", {})
    r, c = s["restart"], s["cleanup"]
    nxt = None
    if r["enabled"]:
        if st.get("restart_key") == json.dumps([r["frequency"], r["weekday"], r["time"]]) and st.get("restart_next"):
            nxt = st["restart_next"]
        else:
            nxt = next_slot(r["frequency"], r["weekday"], r["time"], now).isoformat()
    cnext = next_slot("weekly", c["weekday"], c["time"], now).isoformat() if c["enabled"] else None
    if c["enabled"] and st.get("cleanup_key") == json.dumps([c["weekday"], c["time"]]) and st.get("cleanup_next"):
        cnext = st["cleanup_next"]
    notes = []
    busy = who_is_busy({**r, "wait_minecraft": True, "wait_plex": True}, notes, fresh=False)  # cached: the page polls this
    req, pkgs = reboot_required()
    m = M()
    return {"timezone": str(z), "next_restart": nxt, "waiting": bool(nxt and st.get("restart_waiting") == nxt),
            "busy_now": busy, "notes": notes, "reboot_required": req, "reboot_pkgs": pkgs,
            "uptime": time.time() - psutil.boot_time(), "auth": sys.modules["security"].enabled(),
            "can_reboot": m.HOST_NS and ALLOW_REBOOT, "cleanup_next": cnext, "cleanup_last": st.get("cleanup_last"),
            "open_alerts": [v["title"] for v in (st.get("alerts") or {}).values() if v.get("sent")],
            "quiet_now": quiet_now(s["alerts"]), "held_alerts": len(st.get("held_alerts") or []),
            "can_shutdown": m.HOST_NS and ALLOW_REBOOT}


@router.get("/api/settings")
def api_get_settings():
    """Settings and their status, for the Settings page."""
    s = load_settings()
    return {"settings": _public(s), "status": _status(s)}


ALERT_SWITCHES = ("drives", "pools", "disk_space", "pool_space", "containers", "services", "restarts", "slow", "updates",
                  "servers")


def weakens(cur, new):
    """Changes that would quietly lower the server's guard: where alerts go, alerts turned off, quiet
    hours, which containers get paused, and automatic drive swaps.
    These ask for the password again, so a borrowed sign-in can't switch off the warnings first."""
    a, b = cur["alerts"], new["alerts"]
    out = []
    if a["url"] != b["url"]:
        out.append("where alerts are sent")
    if any(a.get(k) and not b.get(k) for k in ALERT_SWITCHES):
        out.append("turning alerts off")
    if b["disk_threshold"] > a["disk_threshold"] or b["pool_threshold"] > a["pool_threshold"] or b["slow_secs"] > a["slow_secs"]:
        out.append("raising alert limits")
    if b["quiet"] and (not a["quiet"] or (a["quiet_start"], a["quiet_end"]) != (b["quiet_start"], b["quiet_end"])):
        out.append("quiet hours")
    c, d = cur["drivecare"], new["drivecare"]
    if d["auto_replace"] and not c["auto_replace"]:
        out.append("automatic drive replacement")
    if d["pause_containers"] != c["pause_containers"] or (c["hold_restarts"] and not d["hold_restarts"]):
        out.append("what happens during a drive swap")
    return out


@router.put("/api/settings")
def api_put_settings(request: Request, body: dict = Body(...)):
    """Save settings. Changes that would lower the server's guard (see weakens) need the password."""
    def build():
        cur = load_settings()
        try:
            return cur, _clean(body, cur)
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
            raise HTTPException(400, "Some of those settings aren't valid")
    cur, new = build()
    if weakens(cur, new):
        sys.modules["security"].confirm_password(request, body.get("password"))
    with _lock:
        cur, new = build()  # again, in case something changed while the password was checked
        _write("settings.json", new)
    changes = []
    if new["restart"] != cur["restart"]:
        r = new["restart"]
        if not r["enabled"]:
            changes.append("turned scheduled restarts off")
        else:
            when = {"daily": "every day", "weekly": f"every {WEEKDAYS[r['weekday']]}",
                    "monthly": f"on the first {WEEKDAYS[r['weekday']]} of each month"}[r["frequency"]]
            changes.append(f"scheduled restarts {when} at {r['time']}")
    if new["alerts"] != cur["alerts"]:
        changes.append("updated alerts")
    if new["cleanup"] != cur["cleanup"]:
        changes.append(f"weekly Docker clean-up {'on' if new['cleanup']['enabled'] else 'off'}")
    if new["plex"] != cur["plex"]:
        changes.append("updated the Plex connection")
    if new["power"] != cur["power"]:
        changes.append("updated the power estimate")
    if new["updates"] != cur["updates"]:
        u = new["updates"]
        changes.append(f"checking for container updates every {u['hours']} hour{'s' if u['hours'] != 1 else ''}"
                       if u["enabled"] else "turned container update checks off")
    if new["display"]["refresh_ms"] != cur["display"]["refresh_ms"]:
        changes.append(f"live refresh every {new['display']['refresh_ms'] / 1000:g} s")
    if (new["display"]["tiles"], new["display"]["chart"]) != (cur["display"].get("tiles"), cur["display"].get("chart")):
        changes.append("changed the Overview's tiles")
    if new["drivecare"] != cur["drivecare"]:
        changes.append("updated drive care")
        thresholds = ("nvme_hot", "hdd_hot", "wear_warn", "wear_bad")
        if any(new["drivecare"][k] != cur["drivecare"][k] for k in thresholds):
            M()._smart_event.set()  # re-judge the drives with the new limits
    if changes:
        log("settings", "Settings changed: " + "; ".join(changes))
    return {"settings": _public(new), "status": _status(new)}


@router.post("/api/restart")
def api_restart(request: Request, body: dict = Body(...)):
    """Restart the machine now (needs the password)."""
    if body.get("confirm") != "restart":
        raise HTTPException(400, "Missing confirmation")
    if not (M().HOST_NS and ALLOW_REBOOT):
        raise HTTPException(409, "The dashboard isn't set up to restart this machine")
    sys.modules["security"].confirm_password(request, body.get("password"))
    ctx = contextvars.copy_context()  # so the log says who asked
    threading.Thread(target=ctx.run, args=(do_reboot, "restarted from the dashboard"), daemon=True).start()
    return {"ok": True}


@router.post("/api/shutdown")
def api_shutdown(request: Request, body: dict = Body(...)):
    """Shut the machine down now (needs the password: someone has to press the power button to start it again)."""
    if body.get("confirm") != "shutdown":
        raise HTTPException(400, "Missing confirmation")
    if not (M().HOST_NS and ALLOW_REBOOT):
        raise HTTPException(409, "The dashboard isn't set up to shut this machine down")
    sys.modules["security"].confirm_password(request, body.get("password"))  # needs someone at the machine to undo
    reason = "shut down from the dashboard" + (" to swap a drive" if body.get("why") == "swap" else "")
    ctx = contextvars.copy_context()  # so the log says who asked
    threading.Thread(target=ctx.run, args=(do_reboot, reason, True), daemon=True).start()
    return {"ok": True}


@router.post("/api/alerts/test")
def api_alert_test():
    """Send a test alert to the address in Settings."""
    if not load_settings()["alerts"]["url"]:
        raise HTTPException(409, "Add an alert address first (Settings → Alerts)")
    ok, err = notify("Test alert", f"Alerts from the {M().HOST_INFO.get('hostname')} dashboard are working.", "white_check_mark", force=True)
    if not ok:
        raise HTTPException(502, err)
    return {"ok": True}


@router.post("/api/cleanup")
def api_cleanup():
    """Run the Docker clean-up now."""
    try:
        return run_cleanup("run by hand")
    except Exception as e:  # Docker isn't reachable, or refused: its problem, not a fault here
        log("error", f"Docker clean-up failed: {e}")
        raise HTTPException(503, f"Docker didn't do the clean-up: {e}")


@router.get("/api/events")
def api_events(limit: int = Query(100, ge=1, le=500), kind: str = Query(None, max_length=32),
               q: str = Query(None, max_length=200), before: int = Query(None, ge=1)):
    """The activity log, newest first: who did what, and when. `before` (an entry's id) pages back through older
    entries; `kind` and `q` filter them."""
    try:
        return db.events(limit, kind, q, before)
    except Exception as e:
        raise HTTPException(503, f"The activity log isn't available: {e}")
