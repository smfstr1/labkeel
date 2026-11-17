"""The ZFS memory cache: shown apart from used memory, and its limit (Storage → Memory cache → Change limit)."""
import subprocess
from collections import namedtuple
from pathlib import Path

import pytest

import main
import zfscache
import zfsmgr
from conftest import PASSWORD

UI = Path(__file__).resolve().parent.parent / "app" / "static"
GIB = 1 << 30
VM = namedtuple("VM", "total available percent")


class Machine:
    """A stand-in for the machine: the kernel's two files, /etc/modprobe.d, and the commands LabKeel runs there."""

    def __init__(self, tmp, limit=16 * GIB, size=16 * GIB + 200_000_000, files=None, obeys=True):
        self.stats, self.param, self.files, self.obeys, self.ran = tmp / "arcstats", tmp / "zfs_arc_max", dict(files or {}), obeys, []
        self.set(limit, size)

    def set(self, limit, size=None, param=None):
        self.limit = limit
        self.size = size if size is not None else self.size
        self.stats.write_text("13 1 0x01 147 39984 5 9\nname type data\nhits 4 123\n"
                              f"c_min 4 {4 * GIB}\nc_max 4 {limit}\nsize 4 {self.size}\nmemory_free_bytes 4 9\n")
        self.param.write_text(f"{limit if param is None else param}\n")

    def host_cmd(self, args, timeout=15, input=None):
        self.ran.append(args)
        ok = lambda out="": subprocess.CompletedProcess(args, 0, out, "")  # noqa: E731
        if args[0] == "cat":
            path = args[-1]
            return ok(self.files[path]) if path in self.files else subprocess.CompletedProcess(args, 1, "", "No such file")
        if args[0] == "update-initramfs":
            return ok()
        script = args[2]
        if script.startswith("grep -ls"):
            return ok("\n".join(p for p, t in self.files.items() if "zfs_arc_max" in t))
        if script.startswith("printf"):
            n = int(args[4])
            self.set(n if (self.obeys and n) else (64 * GIB if not n else self.limit), param=n)
            return ok()
        if script.startswith("cat >"):
            self.files[args[4]] = input
            return ok()
        raise AssertionError(args)


@pytest.fixture
def box(tmp_path, monkeypatch):
    def make(**kw):
        m = Machine(tmp_path, **kw)
        monkeypatch.setattr(zfscache, "STATS", str(m.stats))
        monkeypatch.setattr(zfscache, "PARAM", str(m.param))
        monkeypatch.setattr(main, "host_cmd", m.host_cmd)
        monkeypatch.setattr(main, "HOST_NS", True)
        monkeypatch.setattr(zfscache.psutil, "virtual_memory", lambda: VM(123 * GIB, 100 * GIB, 18.7))
        monkeypatch.setattr(zfscache.time, "sleep", lambda s: None)
        monkeypatch.setattr(zfscache.threading, "Thread", lambda target, **kw: type("T", (), {"start": lambda self: target()})())
        return m
    return make


def _set(client, limit, password=PASSWORD):
    return client.post("/api/zfs/cache", json={"limit": limit, "password": password})


# --------------------------------------------------------------------------- reading
def test_the_cache_is_not_counted_as_used_memory(box):
    box()
    used, free, pct, arc = zfscache.memory(VM(123 * GIB, 100 * GIB, 18.7))
    assert arc == {"size": 16 * GIB + 200_000_000, "limit": 16 * GIB, "min": 4 * GIB}
    assert free == 100 * GIB + (arc["size"] - 4 * GIB) and used == 123 * GIB - free and 8 < pct < 9.5


def test_a_machine_without_zfs_reports_memory_as_before(monkeypatch):
    monkeypatch.setattr(zfscache, "STATS", "/nonexistent/arcstats")
    assert zfscache.memory(VM(100, 60, 40.0)) == (40, 60, 40.0, None)


def test_the_details_for_an_admin(client, box):
    box(files={"/etc/modprobe.d/zfs.conf": "options zfs zfs_arc_max=17179869184\n"})
    j = client.get("/api/zfs/cache").json()
    assert (j["limit"], j["saved"], j["auto"], j["smallest"], j["largest"]) == (16 * GIB, 16 * GIB, False, 4 * GIB + 1, 122 * GIB)


def test_no_zfs_no_cache(client, monkeypatch):
    monkeypatch.setattr(zfscache, "STATS", "/nonexistent/arcstats")
    assert client.get("/api/zfs/cache").status_code == 404
    assert _set(client, 8 * GIB).status_code == 404


# --------------------------------------------------------------------------- changing
def test_a_new_limit_applies_now_and_is_kept_for_the_next_restart(client, box):
    m = box(files={"/etc/modprobe.d/custom.conf": "# tuned by hand\noptions zfs zfs_arc_max=17179869184 zfs_txg_timeout=10\nblacklist nouveau\n"})
    assert _set(client, 8 * GIB).json() == {"ok": True, "saved": True, "limit": 8 * GIB}
    assert m.param.read_text().strip() == str(8 * GIB)
    assert m.files == {"/etc/modprobe.d/custom.conf": f"# tuned by hand\noptions zfs zfs_arc_max={8 * GIB} zfs_txg_timeout=10\nblacklist nouveau\n"}
    assert ["update-initramfs", "-u"] in m.ran


def test_with_no_file_yet_it_adds_to_zfs_conf_without_losing_what_is_there(client, box):
    m = box(files={"/etc/modprobe.d/zfs.conf": "options zfs zfs_txg_timeout=10"})
    assert _set(client, 8 * GIB).status_code == 200
    assert m.files["/etc/modprobe.d/zfs.conf"] == f"options zfs zfs_txg_timeout=10\noptions zfs zfs_arc_max={8 * GIB}\n"


def test_a_commented_out_limit_is_left_alone(client, box):
    m = box(files={"/etc/modprobe.d/old.conf": "#options zfs zfs_arc_max=1\n"})
    assert _set(client, 8 * GIB).status_code == 200
    assert m.files["/etc/modprobe.d/old.conf"] == "#options zfs zfs_arc_max=1\n"
    assert m.files["/etc/modprobe.d/zfs.conf"] == f"options zfs zfs_arc_max={8 * GIB}\n"


def test_automatic_takes_the_limit_out_of_every_file(client, box):
    m = box(files={"/etc/modprobe.d/a.conf": "options zfs zfs_arc_max=17179869184\n",
                   "/etc/modprobe.d/b.conf": "options zfs zfs_arc_max=17179869184 zfs_txg_timeout=10\n"})
    assert _set(client, 0).json()["saved"] is True
    assert m.param.read_text().strip() == "0"
    assert m.files == {"/etc/modprobe.d/a.conf": "", "/etc/modprobe.d/b.conf": "options zfs zfs_txg_timeout=10\n"}


@pytest.mark.parametrize("limit,why", [(GIB // 2, "smallest limit"), (3 * GIB, "smallest limit"), (123 * GIB, "at most 122 GB"),
                                       (-1, "whole number"), ("8G", "whole number"), (8.5, "whole number"), (True, "whole number"), (None, "whole number")])
def test_limits_that_make_no_sense_are_refused(client, box, limit, why):
    m = box()
    r = _set(client, limit)
    assert r.status_code == 400 and why in r.json()["error"]
    assert m.limit == 16 * GIB and not m.files


def test_it_asks_for_the_password(client, box):
    m = box()
    assert client.post("/api/zfs/cache", json={"limit": 8 * GIB}).status_code == 428
    assert _set(client, 8 * GIB, "wrong").status_code == 403
    assert m.limit == 16 * GIB


def test_off_unless_pool_changes_are_on(client, box, monkeypatch):
    m = box()
    monkeypatch.setattr(zfsmgr, "WRITE", False)
    r = _set(client, 8 * GIB)
    assert r.status_code == 403 and "LABKEEL_ALLOW_ZFS_CHANGES" in r.json()["error"]
    assert m.limit == 16 * GIB and not m.files


def test_not_without_commands_on_the_machine(client, box, monkeypatch):
    m = box()
    monkeypatch.setattr(main, "HOST_NS", False)
    assert _set(client, 8 * GIB).status_code == 409 and m.limit == 16 * GIB


def test_a_limit_zfs_ignores_is_put_back(client, box):
    m = box(obeys=False)
    r = _set(client, 8 * GIB)
    assert r.status_code == 409 and "kept its limit at 16 GB" in r.json()["error"]
    assert m.param.read_text().strip() == str(16 * GIB) and not m.files


def test_a_limit_that_cannot_be_saved_still_applies_and_says_so(client, box, monkeypatch):
    m = box()
    monkeypatch.setattr(zfscache, "_write", lambda path, text: (_ for _ in ()).throw(OSError("read-only file system")))
    assert _set(client, 8 * GIB).json() == {"ok": True, "saved": False, "limit": 8 * GIB}
    assert ["update-initramfs", "-u"] not in m.ran


def test_signed_out_or_a_viewer_gets_nowhere(anon, box):
    box()
    assert anon.get("/api/zfs/cache").status_code in (401, 403)
    assert anon.post("/api/zfs/cache", json={"limit": 8 * GIB}).status_code in (401, 403)
    import security
    assert not security.viewer_allowed("GET", "/api/zfs/cache") and not security.viewer_allowed("POST", "/api/zfs/cache")


# --------------------------------------------------------------------------- the page
def test_the_pages_show_it():
    js, html = (UI / "ui" / "storage.js").read_text(), (UI / "index.html").read_text()
    assert 'id="s-cache-sec"' in html and 'id="s-cache-tools"' in html and "ZFS keeps recently" not in html
    assert "S.dc?.zfs?.write" in js and '"/api/zfs/cache"' in js and "Automatic: let ZFS decide" in js
    assert "ZFS cache" in (UI / "ui" / "overview.js").read_text()
