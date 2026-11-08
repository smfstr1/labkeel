"""Every route, given badly shaped input by a signed-in admin, answers with a clear error and never crashes (500;
a 502 or 503 is a deliberate "the other end didn't answer").

The sweep calls every route, so it runs with the machine out of reach: no Docker, no commands, nothing from the
network. Without that, a route like Run Docker clean-up would really prune the images of whoever runs the tests.
The bodies are the wrong type altogether, the right shape with each field the wrong type, broken JSON, and JSON
nested deeper than Python can read."""
import json
import re
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient

import apps
import containers
import main
import updates
from conftest import PASSWORD, USER
from test_security import _flat

# signing out (here, or everywhere) would end the sweep's own session part-way
SKIP = {"/api/logout", "/api/logout-all"}
WRONG = [[], ["x"], "x", 7, -1, 1e308, True, None, {"a": 1}, "x" * 5000, "../../etc/passwd", "\x00"]


def _keys(fn):
    """The body fields a route reads: body.get("x") / body["x"] in its source."""
    import inspect
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):
        return []
    return sorted(set(re.findall(r'body(?:\.get\(|\[)"([a-z_]+)"', src)))


def _cases():
    out = []
    for r in _flat(main.app.routes):
        methods = (getattr(r, "methods", None) or set()) - {"HEAD", "OPTIONS"}
        if not methods or r.path in SKIP or not r.path.startswith("/api/"):
            continue
        for m in sorted(methods):
            out.append((m, r.path, _keys(getattr(r, "endpoint", None))))
    return out


CASES = _cases()


def _fill(path):
    return re.sub(r"\{[^}]+\}", "x", path)


RAN = []  # anything the sweep tried to run on the machine


def _no_docker():
    raise RuntimeError("Docker isn't reachable")


def _no_command(cmd, timeout=15, input=None):
    RAN.append(cmd)
    return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not run during the sweep")


def _no_download(p):
    raise RuntimeError("no network during the sweep")


@pytest.fixture(scope="module")
def admin():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(containers, "dclient", _no_docker)
        mp.setattr(main, "run_bounded", _no_command)
        mp.setattr(updates, "_transport", lambda: httpx.MockTransport(lambda req: httpx.Response(503)))
        mp.setattr(apps, "_fetch_upstream", _no_download)
        c = TestClient(main.app, base_url="https://localhost", raise_server_exceptions=False)
        assert c.post("/api/login", json={"username": USER, "password": PASSWORD}).status_code == 200
        yield c


def test_the_sweep_cannot_reach_the_machine(admin):
    """Run Docker clean-up, through the sweep's client: nothing reaches Docker or the machine."""
    assert admin.post("/api/cleanup", json={"password": PASSWORD}).status_code != 200
    assert containers.dclient is _no_docker and main.run_bounded is _no_command


def _bodies(keys):
    yield from ([], "x", 7, None)                                       # not an object at all
    yield {"password": PASSWORD}
    for k in keys:                                                      # each field the wrong type, in turn
        for bad in WRONG:
            yield {"password": PASSWORD, **{o: "x" for o in keys if o != k and o != "password"}, k: bad}


@pytest.mark.parametrize("method,path,keys", CASES, ids=[f"{m} {p}" for m, p, _ in CASES])
def test_bad_input_never_crashes(admin, browse, method, path, keys):
    url = _fill(path)
    crashed = []
    if method == "GET":
        for q in ("", "?path=../..", "?path=%00", "?path=" + "a/" * 3000, "?since=nan", "?since=x", "?limit=-5",
                  "?limit=99999999999999999999", "?q=" + "(" * 2000, "?id=x&kind=x&range=x"):
            r = admin.get(url + q)
            if r.status_code == 500:
                crashed.append((q[:40], r.status_code, r.text[:200]))
    else:
        for b in _bodies(keys):
            r = admin.request(method, url, json=b)
            if r.status_code == 500:
                crashed.append((json.dumps(b)[:80], r.status_code, r.text[:200]))
        for raw in (b"{", b"\xff\xfe", b"[" * 3000 + b"]" * 3000, b"[" * 100_000 + b"]" * 100_000,
                    b'{"a":' * 50_000 + b"1" + b"}" * 50_000):
            r = admin.request(method, url, content=raw, headers={"Content-Type": "application/json"})
            if r.status_code == 500:
                crashed.append((repr(raw[:12]), r.status_code, r.text[:200]))
    assert not crashed, crashed
