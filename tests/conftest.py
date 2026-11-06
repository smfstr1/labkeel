"""
Shared set-up for the tests.

The app reads its settings from environment variables when it's imported, so they're set here,
before anything imports it, pointing at a throwaway folder. Nothing touches the real machine:
LABKEEL_HOST_COMMANDS=0 keeps every command inside this process's own view of the system.
"""
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

TMP = Path(tempfile.mkdtemp(prefix="labkeel-test-"))
for d in ("data", "config", "certs", "browse"):
    (TMP / d).mkdir()
(TMP / "config" / "config.json").write_text('{"title": "Test lab"}')

USER, PASSWORD = "admin", "correct-horse-battery-staple"

from argon2 import PasswordHasher  # noqa: E402

PASSWORD_HASH = PasswordHasher().hash(PASSWORD)

os.environ.update({
    "LABKEEL_DATA_DIR": str(TMP / "data"),
    "LABKEEL_CONFIG_FILE": str(TMP / "config" / "config.json"),
    "LABKEEL_CERTS_DIR": str(TMP / "certs"),
    "LABKEEL_FILES_DIR": str(TMP / "browse"),
    "LABKEEL_HOST_COMMANDS": "0",
    "LABKEEL_ADMIN_USER": USER,
    "LABKEEL_ADMIN_PASSWORD_HASH": PASSWORD_HASH,
    "LABKEEL_ALLOW_RESTART": "0",
    "LABKEEL_ALLOW_FILE_CHANGES": "1",
    "LABKEEL_ALLOW_ZFS_CHANGES": "1",
})

import main  # noqa: E402
import security  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

BROWSE = TMP / "browse"


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(TMP, ignore_errors=True)


@pytest.fixture(autouse=True)
def fresh_limits():
    """Each test starts with no wrong-password history and no confirmed passwords."""
    with security._lock:
        security._fails.clear()
        security._all_fails.clear()
        security._elevated.clear()
    yield


@pytest.fixture
def anon():
    return TestClient(main.app, base_url="https://localhost")


@pytest.fixture
def client():
    c = TestClient(main.app, base_url="https://localhost")
    r = c.post("/api/login", json={"username": USER, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture
def browse():
    """A clean set of folders in the file browser for each test."""
    for p in BROWSE.iterdir():
        shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
    (BROWSE / "media").mkdir()
    (BROWSE / "media" / "notes.txt").write_text("hello\n")
    return BROWSE


@pytest.fixture
def config():
    """Write config.json for one test; put the plain one back afterwards."""
    path = TMP / "config" / "config.json"

    def write(text):
        path.write_text(text)
        main._svc_cache.update(data=None, t=0)

    yield write
    write('{"title": "Test lab"}')


@pytest.fixture
def no_accounts(monkeypatch, tmp_path):
    """No accounts at all: no admin from the environment and an empty users.json in a folder of its own."""
    import accounts
    monkeypatch.setattr(accounts, "_env", lambda: None)
    monkeypatch.setattr(accounts, "DATA_DIR", tmp_path)
    accounts._cache.update(sig=None, users=[])
    accounts._setup["code"] = None
    yield accounts
    accounts._cache.update(sig=None, users=[])
    accounts._setup["code"] = None


@pytest.fixture
def users(monkeypatch, tmp_path):
    """The environment's admin, plus a users.json of this test's own to add accounts to."""
    import accounts
    monkeypatch.setattr(accounts, "DATA_DIR", tmp_path)
    accounts._cache.update(sig=None, users=[])
    yield accounts
    accounts._cache.update(sig=None, users=[])


def sign_in(name, password, client=None, **kw):
    c = client or TestClient(main.app, base_url="https://localhost", **kw)
    r = c.post("/api/login", json={"username": name, "password": password})
    assert r.status_code == 200, r.text
    return c


# --------------------------------------------------------------------------- a real Docker, when there is one
# Tiny containers made just for the tests: from the image labkeel-test/ticker:1 if it's been built locally,
# otherwise busybox (pulled from Docker Hub, as on GitHub's test machines). Tests using them are skipped
# where there's no Docker.
TICK = ["/bin/sh", "-c", "i=0; while true; do echo tick $i; i=$((i+1)); sleep 1; done"]


def _docker():
    import containers
    try:
        cl = containers.dclient()
        cl.ping()
        return cl
    except Exception:
        return None


def _image(cl):
    for ref in ("labkeel-test/ticker:1", "busybox:1.36"):
        try:
            cl.images.get(ref)
            return ref
        except Exception:
            pass
    try:
        cl.images.pull("busybox", tag="1.36")
        return "busybox:1.36"
    except Exception:
        return None


@pytest.fixture
def docker_cl():
    cl = _docker()
    if not cl or not _image(cl):
        pytest.skip("no Docker (or no test image) here")
    return cl


@pytest.fixture
def ticker(docker_cl):
    """A running container that prints a line a second. Removed afterwards, whatever happens."""
    made = []

    def make(prefix="lk-test"):
        c = docker_cl.containers.run(_image(docker_cl), TICK, name=f"{prefix}-{uuid.uuid4().hex[:8]}", detach=True,
                                     labels={"labkeel.test": "1"})
        made.append(c)
        for _ in range(50):
            c.reload()
            if c.status == "running":
                break
            time.sleep(0.1)
        return c
    yield make
    for c in made:
        try:
            c.remove(force=True)
        except Exception:
            pass
