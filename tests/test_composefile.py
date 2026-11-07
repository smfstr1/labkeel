"""Bringing docker-compose.yml up to date (composefile.py): what's added and renamed, that nothing else in the
file changes, the checks before it's saved, and that it runs after an update."""
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import composefile
import main
import selfupdate
from conftest import PASSWORD

yaml = pytest.importorskip("yaml")
ROOT = Path(__file__).resolve().parent.parent

# a compose file from before 0.1.2: old names, and none of the newer switches
OLD = """\
# my LabKeel
services:
  labkeel:
    build: .
    container_name: labkeel
    privileged: true

    environment:
      - DASH_FILES_WRITE=1        # I want uploads
      - DASH_PORT=8787
      - "DASH_PASS_HASH=$$argon2id$$v=19$$abc"
      # - DASH_HSTS=1
      - TZ=Europe/London

    volumes:
      - ./data:/data
  other:
    image: nginx
    environment:
      - DASH_PORT=1
"""


def resolved(text, service="labkeel"):
    """What Docker Compose would give the service: the environment, with $$ read as $."""
    e = yaml.safe_load(text)["services"][service].get("environment") or {}
    if isinstance(e, list):
        e = dict((x.split("=", 1) + [None])[:2] for x in e)
    return {k: (None if v is None else str(v).replace("$$", "$")) for k, v in e.items()}


def test_old_names_renamed_and_new_settings_added():
    p = composefile.plan(OLD, "labkeel", resolved(OLD))
    assert p["changed"]
    assert [r["old"] for r in p["renamed"]] == ["DASH_FILES_WRITE", "DASH_PORT", "DASH_PASS_HASH"]
    # LABKEEL_ALLOW_FILE_CHANGES is already given (by its old name), so only the other three are added
    assert [a["name"] for a in p["added"]] == ["LABKEEL_ALLOW_ZFS_CHANGES", "LABKEEL_ALLOW_RESTART",
                                               "LABKEEL_ALLOW_APP_INSTALLS"]
    new = p["text"]
    assert "      - LABKEEL_ALLOW_FILE_CHANGES=1        # I want uploads\n" in new          # value and note kept
    assert '      - "LABKEEL_ADMIN_PASSWORD_HASH=$$argon2id$$v=19$$abc"\n' in new
    assert "      # - DASH_HSTS=1\n" in new                                                  # comments left alone
    assert "      - DASH_PORT=1\n" in new                                                    # other services too
    # the additions go after the last setting, before the blank line and volumes
    after_tz = new.split("      - TZ=Europe/London\n", 1)[1]
    assert after_tz.startswith("      # Added by LabKeel") and "      - LABKEEL_ALLOW_RESTART=0" in after_tz.split("volumes:")[0]
    assert "# 1 = add and remove apps on the Apps page" in new
    env = resolved(new)
    assert env == {"LABKEEL_ALLOW_FILE_CHANGES": "1", "LABKEEL_PORT": "8787",
                   "LABKEEL_ADMIN_PASSWORD_HASH": "$argon2id$v=19$abc", "TZ": "Europe/London",
                   "LABKEEL_ALLOW_ZFS_CHANGES": "0", "LABKEEL_ALLOW_RESTART": "0", "LABKEEL_ALLOW_APP_INSTALLS": "0"}
    # everything else is exactly as it was: take the additions out and undo the renames, and it's the old file
    undone = "".join(ln for ln in new.splitlines(keepends=True)
                     if not (ln.startswith("      # Added by LabKeel") or any(f"- {a['name']}=" in ln for a in p["added"])))
    for r in p["renamed"]:
        undone = undone.replace(r["new"], r["old"], 1)
    assert undone == OLD
    assert composefile.plan(new, "labkeel", resolved(new))["changed"] is False    # and once is enough


def test_the_example_is_already_up_to_date():
    text = (ROOT / "docker-compose.example.yml").read_text()
    assert composefile.plan(text, "labkeel", resolved(text))["changed"] is False
    # and LISTED is the example's own lines
    for name, value, note in composefile.LISTED:
        assert any(line.strip().startswith(f"- {name}={value} ") and line.rstrip().endswith(f"# {note}")
                   for line in text.splitlines()), name


def test_mapping_style_and_windows_line_endings():
    text = "services:\r\n  labkeel:\r\n    environment:\r\n      DASH_PORT: 8787\r\n      'TZ': Europe/London\r\n"
    p = composefile.plan(text, "labkeel", resolved(text))
    assert "      LABKEEL_PORT: 8787\r\n" in p["text"] and '      LABKEEL_ALLOW_RESTART: "0"' in p["text"]
    assert "\n" not in p["text"].replace("\r\n", "")
    assert resolved(p["text"])["LABKEEL_ALLOW_APP_INSTALLS"] == "0"


def test_settings_given_elsewhere_count():
    # from an env_file or an override file: resolved has them, the text doesn't
    others = {"LABKEEL_ALLOW_FILE_CHANGES": "1", "LABKEEL_PORT": "9000", "DASH_ZFS_WRITE": "1"}
    p = composefile.plan(OLD, "labkeel", {**resolved(OLD), **others})
    assert "LABKEEL_ALLOW_FILE_CHANGES" not in [a["name"] for a in p["added"]]
    assert "LABKEEL_ALLOW_ZFS_CHANGES" not in [a["name"] for a in p["added"]]
    assert "DASH_PORT" not in [r["old"] for r in p["renamed"]]           # renaming it would override 9000
    assert any("DASH_PORT does nothing" in n for n in p["notes"])


def test_quiet_settings_and_values_on_two_lines():
    text = "services:\n  labkeel:\n    environment:\n    - DASH_PORT\n    - LABKEEL_HOSTNAMES=a,\n      b\n    ports: []\n"
    p = composefile.plan(text, "labkeel", {"DASH_PORT": None, "LABKEEL_HOSTNAMES": "a, b"})
    lines = p["text"].splitlines()
    assert lines[3] == "    - LABKEEL_PORT" and lines[5] == "      b" and lines[6].startswith("    # Added")
    assert lines[-1] == "    ports: []"


@pytest.mark.parametrize("text, words", [
    ("services:\n  labkeel:\n    environment: [DASH_PORT=1]\n", "one line"),
    ("services:\n  labkeel:\n    image: x\n", "no environment"),
    ("services:\n  web:\n    environment:\n      - A=1\n", "isn't in it"),
    ("version: '3'\n", "no services"),
])
def test_files_it_wont_rewrite(text, words):
    with pytest.raises(composefile.CantTidy, match=words):
        composefile.plan(text, "labkeel", {})


# --------------------------------------------------------------------------- on the machine
class R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


@pytest.fixture
def machine(monkeypatch):
    """docker-compose.yml in /home/sam/labkeel, with the host's files and Docker Compose stood in for."""
    path = "/home/sam/labkeel/docker-compose.yml"
    files, cmds = {path: OLD}, []
    info = {"project": "labkeel", "workdir": "/home/sam/labkeel", "service": "labkeel", "files": [path]}

    def host(args, timeout=30, input=None):
        cmds.append(args)
        if args[0] == "cat":
            return R(0, files[args[-1]]) if args[-1] in files else R(1, err="No such file")
        if args[0] == "test":
            return R(1)
        if args[:2] == ["sh", "-c"] and "chown" in args[2]:
            files[args[4]] = input
            return R()
        if args[:2] == ["sh", "-c"] and "mv -f" in args[2]:
            p, new = args[4], args[5]
            files[p + ".bak"], files[p] = files[p], files.pop(new)
            return R()
        if args[0] == "rm":
            files.pop(args[-1], None)
            return R()
        if "config" in args:
            f = args[args.index("-f") + 1]
            return R(0, json.dumps({"services": {"labkeel": {"environment": resolved(files[f])}}}))
        return R(127, err=f"no fake answer for {args}")
    monkeypatch.setattr(selfupdate, "host", host)
    monkeypatch.setattr(selfupdate, "install_info", lambda fresh=False: (info, None))
    return files, cmds, path


def test_bringing_it_up_to_date(machine):
    files, cmds, path = machine
    res = composefile.apply()
    assert res["changed"] and res["backup"] == path + ".bak"
    assert files[path + ".bak"] == OLD and "LABKEEL_ALLOW_APP_INSTALLS=0" in files[path]
    assert not [f for f in files if "labkeel-new" in f]
    write = next(c for c in cmds if c[:2] == ["sh", "-c"] and "chown" in c[2])
    assert "--reference" in write[2] and write[5] == path            # keeps your ownership and permissions
    assert composefile.apply()["changed"] is False                     # nothing more to do
    assert "Brought /home/sam/labkeel/docker-compose.yml up to date: added LABKEEL_ALLOW_ZFS_CHANGES=0" in \
        composefile.describe(res)


def test_left_alone_if_compose_reads_it_differently(machine, monkeypatch):
    files, cmds, path = machine
    real = selfupdate.host

    def host(args, timeout=30, input=None):
        r = real(args, timeout, input)
        if "config" in args and "labkeel-new" in args[args.index("-f") + 1]:
            env = json.loads(r.stdout)["services"]["labkeel"]["environment"]
            env["LABKEEL_PORT"] = "1"
            r.stdout = json.dumps({"services": {"labkeel": {"environment": env}}})
        return r
    monkeypatch.setattr(selfupdate, "host", host)
    with pytest.raises(composefile.CantTidy, match="left as it was"):
        composefile.apply()
    assert files[path] == OLD and path + ".bak" not in files and not [f for f in files if "labkeel-new" in f]


def test_left_alone_if_you_were_editing_it(machine, monkeypatch):
    files, cmds, path = machine
    real = selfupdate.host
    reads = []

    def host(args, timeout=30, input=None):
        if args[0] == "cat":
            reads.append(1)
            if len(reads) == 2:
                files[path] = OLD + "# a line you just added\n"
        return real(args, timeout, input)
    monkeypatch.setattr(selfupdate, "host", host)
    with pytest.raises(composefile.CantTidy, match="changed while"):
        composefile.apply()
    assert files[path].endswith("# a line you just added\n") and path + ".bak" not in files


def test_from_settings(machine, client, users):
    files, cmds, path = machine
    st = client.get("/api/labkeel/compose").json()
    assert st["possible"] and st["changed"] and len(st["added"]) == 3 and files[path] == OLD   # only looked
    assert client.post("/api/labkeel/compose/tidy", json={}).status_code == 428           # password again
    r = client.post("/api/labkeel/compose/tidy", json={"password": PASSWORD})
    assert r.status_code == 200 and r.json()["changed"]
    assert client.get("/api/events", params={"q": "up to date"}).json()[0]["kind"] == "settings"
    users.create("watcher", "viewer", "a-long-viewer-password")
    v = TestClient(main.app, base_url="https://localhost")
    v.post("/api/login", json={"username": "watcher", "password": "a-long-viewer-password"})
    assert v.get("/api/labkeel/compose").status_code == 403


def test_after_an_update(machine, client):
    files, cmds, path = machine
    (main.DATA_DIR / "update.json").write_text(json.dumps({"last": {"t": time.time(), "ok": True}}))
    try:
        selfupdate._tidy_after("sam-admin")
        last = client.get("/api/labkeel/update").json()["last"]
        assert last["compose"]["ok"] and last["compose"]["changed"] and last["compose"]["backup"] == path + ".bak"
        e = client.get("/api/events", params={"q": "up to date"}).json()[0]
        assert e["user"] == "sam-admin" and e["kind"] == "update"
    finally:
        (main.DATA_DIR / "update.json").unlink(missing_ok=True)
