"""
Keeping your docker-compose.yml up to date.

docker-compose.yml is your own copy of docker-compose.example.yml, and git leaves it alone because it holds
your settings. So when LabKeel updates, new settings only reach the example. This brings your copy up to date,
changing as little as it can:

  - settings every copy should list (the switches for what LabKeel may change) are added at their default,
    which is off, with the example's note beside them
  - settings still given by their old names (DASH_...) get their new names (LABKEEL_...), values untouched

Nothing else changes: not your values, comments, order or layout. Before saving, Docker Compose reads the new
file, and LabKeel checks that the environment it gives LabKeel is exactly the old one plus those additions and
renames. If it isn't, or the file changed meanwhile, the file is left alone. The old one is kept as
docker-compose.yml.bak.

It runs after LabKeel updates itself (selfupdate.py), and from Settings → LabKeel for anyone who updates by
hand. Adding settings at their defaults and renaming them changes nothing LabKeel does, so it doesn't restart:
the next `docker compose up -d` picks the file up.
"""
import json
import re
import sys
import threading
from pathlib import PurePosixPath

from fastapi import APIRouter, Body, HTTPException, Request

import env
from version import VERSION

router = APIRouter()

# Settings every docker-compose.yml should list, so they're there to find and change: (name, default, note).
# The same lines as docker-compose.example.yml (a test checks).
LISTED = [
    ("LABKEEL_ALLOW_FILE_CHANGES", "0", "1 = upload, edit, move and delete files in the file browser"),
    ("LABKEEL_ALLOW_ZFS_CHANGES", "0", "1 = create, change and destroy ZFS pools, datasets and snapshots"),
    ("LABKEEL_ALLOW_RESTART", "0", "1 = restart and shut down the machine"),
    ("LABKEEL_ALLOW_APP_INSTALLS", "0", "1 = add and remove apps on the Apps page (docs/apps.md)"),
]
NEW_NAME = {old: new for new, old in env.RENAMED.items()}
_lock = threading.Lock()


class CantTidy(Exception):
    """The file can't be brought up to date automatically; the message says why, for the page."""


def S():
    return sys.modules["selfupdate"]


def U():
    return sys.modules["upkeep"]


# --------------------------------------------------------------------------- reading the file's text
_KEY = r"[A-Za-z_][A-Za-z0-9_]*"
_LIST_ENTRY = re.compile(rf"^(\s*-\s*[\"']?)({_KEY})(?=[=\s\"']|$)")
_MAP_ENTRY = re.compile(rf"^(\s*[\"']?)({_KEY})([\"']?\s*:)")


def _content(line):
    """The line without its ending, or None for a blank line or a comment."""
    s = line.rstrip("\r\n")
    return None if not s.strip() or s.lstrip().startswith("#") else s


def _indent(s):
    return len(s) - len(s.lstrip(" "))


def _key_line(lines, start, end, indent, key):
    """Index of `key:` at exactly this indentation between start and end, or None."""
    pat = re.compile(rf"^ {{{indent}}}[\"']?{re.escape(key)}[\"']?\s*:(.*)$")
    for i in range(start, end):
        s = _content(lines[i])
        if s is not None and _indent(s) == indent and pat.match(s):
            return i
    return None


def _block_end(lines, start, indent):
    """The end (exclusive) of the block whose children start after line `start` and sit deeper than `indent`."""
    i = start + 1
    while i < len(lines):
        s = _content(lines[i])
        if s is not None and _indent(s) <= indent:
            return i
        i += 1
    return i


def _child_indent(lines, start, end):
    for i in range(start + 1, end):
        s = _content(lines[i])
        if s is not None:
            return _indent(s)
    return None


def find_env(lines, service):
    """Where LabKeel's environment section is: {"key": line of `environment:`, "entries": [(first line, last
    line, key)], "style": "list" | "map", "indent": the entries' indentation, "end": the section's end}."""
    top = _key_line(lines, 0, len(lines), 0, "services")
    if top is None:
        raise CantTidy("It has no services: section that LabKeel could find.")
    services_end = _block_end(lines, top, 0)
    si = _child_indent(lines, top, services_end)
    svc = _key_line(lines, top + 1, services_end, si, service) if si else None
    if svc is None:
        raise CantTidy(f"LabKeel's service ({service}) isn't in it, so it may be in another compose file.")
    svc_end = _block_end(lines, svc, si)
    ci = _child_indent(lines, svc, svc_end)
    k = _key_line(lines, svc + 1, svc_end, ci, "environment") if ci else None
    if k is None:
        raise CantTidy("LabKeel's service has no environment: section to add to.")
    rest = _content(lines[k]).split(":", 1)[1].split("#", 1)[0].strip()
    if rest:
        raise CantTidy("Its environment: section is written on one line, which LabKeel won't rewrite.")
    entries, style, eind, end = [], None, None, k + 1
    i = k + 1
    while i < len(lines):
        s = _content(lines[i])
        if s is None:
            i += 1
            continue
        ind = _indent(s)
        if ind < ci or (ind == ci and not s.lstrip().startswith("-")):
            break
        if eind is None:
            eind, style = ind, "list" if s.lstrip().startswith("-") else "map"
        if ind == eind:
            m = (_LIST_ENTRY if style == "list" else _MAP_ENTRY).match(s)
            if not m:
                raise CantTidy(f"Line {i + 1} of its environment: section isn't a setting LabKeel can read.")
            entries.append([i, i, m.group(2)])
        elif ind > eind and entries:
            entries[-1][1] = i  # a value carried on to the next line
        else:
            raise CantTidy(f"Line {i + 1} of its environment: section is indented in a way LabKeel won't rewrite.")
        end = i + 1
        i += 1
    return {"key": k, "entries": entries, "style": style or "list", "indent": eind if eind is not None else ci + 2,
            "end": end}


# --------------------------------------------------------------------------- what to change
def plan(text, service, resolved):
    """The new text, and what changed. `resolved` is the environment Docker Compose gives LabKeel now (from
    every compose file, env_file and .env), so a setting given anywhere counts as given."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += nl
    where = find_env(lines, service)
    here = {key for _, _, key in where["entries"]}
    renamed, notes = [], []
    for first, _, key in where["entries"]:
        new = NEW_NAME.get(key)
        if not new:
            continue
        if new in resolved or new in here:
            notes.append(f"{key} does nothing now that {new} is set; you can delete its line.")
            continue
        pat = _LIST_ENTRY if where["style"] == "list" else _MAP_ENTRY
        m = pat.match(lines[first])
        lines[first] = lines[first][:m.start(2)] + new + lines[first][m.end(2):]
        renamed.append({"old": key, "new": new})
        here.add(new)
    added = [{"name": n, "value": v, "note": note} for n, v, note in LISTED
             if n not in resolved and n not in here and env.RENAMED.get(n) not in resolved]
    if added:
        ind = " " * where["indent"]
        if where["style"] == "list":
            body = [f"- {a['name']}={a['value']}" for a in added]
        else:
            body = [f"{a['name']}: \"{a['value']}\"" for a in added]
        width = max(len(b) for b in body) + 3
        new_lines = [f"{ind}# Added by LabKeel {VERSION}, switched off (see docs/settings.md):{nl}"]
        new_lines += [f"{ind}{b.ljust(width)}# {a['note']}{nl}" for b, a in zip(body, added)]
        at = where["entries"][-1][1] + 1 if where["entries"] else where["key"] + 1
        lines[at:at] = new_lines
    return {"text": "".join(lines), "renamed": renamed, "added": added, "notes": notes,
            "changed": bool(renamed or added)}


# --------------------------------------------------------------------------- on the machine
def _main_file(info):
    return info["files"][0]


def _resolved(info, files):
    """The environment Docker Compose would give LabKeel's service with these compose files."""
    args = ["docker", "compose", "-p", info["project"], "--project-directory", info["workdir"]]
    for f in files:
        args += ["-f", f]
    r = S().host(args + ["config", "--format", "json"], 30)
    if r.returncode != 0:
        raise CantTidy(f"Docker Compose couldn't read it: {S()._err(r)[0]}")
    try:
        e = json.loads(r.stdout)["services"][info["service"]].get("environment") or {}
    except (ValueError, KeyError, TypeError, AttributeError) as ex:
        raise CantTidy("Docker Compose's answer about it couldn't be read.") from ex
    if isinstance(e, list):
        e = dict((x.split("=", 1) + [None])[:2] for x in e)
    return {str(k): (None if v is None else str(v)) for k, v in e.items()}


def _read(path):
    r = S().host(["cat", "--", path], 15)
    if r.returncode != 0:
        raise CantTidy(f"Couldn't read {path}: {S()._err(r)[0]}")
    return r.stdout


def _install(fresh=False):
    info, why = S().install_info(fresh)
    if not info:
        raise CantTidy(why)
    path = _main_file(info)
    if S().host(["test", "-L", path], 10).returncode == 0:
        raise CantTidy(f"{path} is a link to another file, which LabKeel won't replace.")
    return info, path


def examine():
    """What bringing the file up to date would change (nothing is written)."""
    info, path = _install()
    p = plan(_read(path), info["service"], _resolved(info, info["files"]))  # nothing written
    return {"file": path, **{k: p[k] for k in ("renamed", "added", "notes", "changed")}}


def apply():
    """Bring the file up to date. Returns what changed; raises CantTidy, with the file untouched, if anything
    doesn't come out exactly as expected."""
    with _lock:
        info, path = _install(fresh=True)
        before = _read(path)
        old_env = _resolved(info, info["files"])
        p = plan(before, info["service"], old_env)
        result = {"file": path, **{k: p[k] for k in ("renamed", "added", "notes", "changed")}, "backup": None}
        if not p["changed"]:
            return result
        folder, name = str(PurePosixPath(path).parent), PurePosixPath(path).name
        new = f"{folder}/.{name}.labkeel-new"
        # the new file gets the old one's owner and permissions, so you can still edit it as yourself
        r = S().host(["sh", "-c", 'cat > "$1" && chown --reference="$2" "$1" && chmod --reference="$2" "$1"',
                      "sh", new, path], 20, input=p["text"])
        try:
            if r.returncode != 0:
                raise CantTidy(f"Couldn't write next to it: {S()._err(r)[0]}")
            got = _resolved(info, [new, *info["files"][1:]])
            ren = {x["old"]: x["new"] for x in p["renamed"]}
            want = {**{ren.get(k, k): v for k, v in old_env.items()}, **{a["name"]: a["value"] for a in p["added"]}}
            if got != want:
                raise CantTidy("Docker Compose didn't read the new file as expected, so it was left as it was.")
            if _read(path) != before:
                raise CantTidy(f"{path} changed while LabKeel was working on it. Try again.")
            r = S().host(["sh", "-c", 'cp -p -- "$1" "$1.bak" && mv -f -- "$2" "$1"', "sh", path, new], 20)
            if r.returncode != 0:
                raise CantTidy(f"Couldn't replace it: {S()._err(r)[0]}")
        except CantTidy:
            S().host(["rm", "-f", "--", new], 10)
            raise
        result["backup"] = f"{path}.bak"
        return result


def describe(res):
    """One line for the activity log."""
    parts = []
    if res["added"]:
        parts.append("added " + ", ".join(f"{a['name']}={a['value']}" for a in res["added"]))
    if res["renamed"]:
        parts.append("renamed " + ", ".join(f"{r['old']} to {r['new']}" for r in res["renamed"]))
    return f"Brought {res['file']} up to date: {'; '.join(parts)}. The old one is {res['backup']}."


def after_update():
    """Called once an update has finished (as whoever asked for it): tidy the file, and note how it went."""
    if not S().install_info(fresh=True)[0]:
        return None  # updated by hand, or can't run commands on the machine: Settings says what to do
    try:
        res = apply()
    except CantTidy as e:
        U().log("error", f"After updating, LabKeel couldn't bring docker-compose.yml up to date: {e}")
        return {"ok": False, "detail": str(e)}
    except Exception as e:
        U().log("error", f"After updating, LabKeel couldn't bring docker-compose.yml up to date: {type(e).__name__}: {e}")
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}
    if res["changed"]:
        U().log("update", describe(res))
    return {"ok": True, **res}


# --------------------------------------------------------------------------- the API (admins only)
@router.get("/api/labkeel/compose")
def api_compose():
    try:
        return {"possible": True, **examine()}
    except CantTidy as e:
        return {"possible": False, "reason": str(e)}


@router.post("/api/labkeel/compose/tidy")
def api_compose_tidy(request: Request, body: dict = Body(...)):
    """Bring docker-compose.yml up to date now (password again)."""
    sys.modules["security"].confirm_password(request, body.get("password"))
    try:
        res = apply()
    except CantTidy as e:
        raise HTTPException(409, str(e)) from e
    if res["changed"]:
        U().log("settings", describe(res))
    return res
