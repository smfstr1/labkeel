"""
Shared set-up for the tests.

The app reads its settings from environment variables when it's imported, so they're set here,
before anything imports it, pointing at a throwaway folder. Nothing touches the real machine:
DASH_HOST_NS=0 keeps every command inside this process's own view of the system.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

TMP = Path(tempfile.mkdtemp(prefix="dash-test-"))
for d in ("data", "config", "certs", "browse"):
    (TMP / d).mkdir()
(TMP / "config" / "config.json").write_text('{"title": "Test lab"}')

USER, PASSWORD = "admin", "correct-horse-battery-staple"

os.environ.update({
    "DASH_DATA": str(TMP / "data"),
    "DASH_CONFIG": str(TMP / "config" / "config.json"),
    "DASH_CERTS": str(TMP / "certs"),
    "DASH_BROWSE_ROOT": str(TMP / "browse"),
    "DASH_HOST_NS": "0",
    "DASH_USER": USER,
    "DASH_PASS": PASSWORD,
    "DASH_ALLOW_REBOOT": "0",
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
