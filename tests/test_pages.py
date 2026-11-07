"""The dashboard page and its own scripts and styles (static/ui), and the security policy that lets only
those run."""
import re


UI = ["core", "overview", "containers", "storage", "zfs", "services", "apps", "servers", "files", "panes", "dialogs", "settings", "main"]


def test_page_links_every_script_in_order_with_a_version(client):
    html = client.get("/").text
    found = re.findall(r'<script src="/ui/([a-z]+)\.js\?v=([0-9a-f]{12})"></script>', html)
    assert [n for n, _ in found] == UI
    assert re.search(r'href="/ui/app\.css\?v=[0-9a-f]{12}"', html)
    assert "<script>" not in html and "<style>" not in html   # nothing inline left


def test_only_the_dashboards_own_scripts_may_run(client, anon):
    csp = client.get("/").headers["content-security-policy"]
    assert "script-src 'self';" in csp and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    login = anon.get("/login").headers["content-security-policy"]
    assert "'self'" not in login.split("script-src")[1].split(";")[0]   # pages with inline scripts stay hash-only


def test_ui_files(client):
    for n in UI:
        r = client.get(f"/ui/{n}.js")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript") and '"use strict"' in r.text
        assert "immutable" in r.headers["cache-control"]
    assert client.get("/ui/app.css").headers["content-type"].startswith("text/css")
    for bad in ("nope.js", "core.py", "CORE.js", "..%2Fmain.py"):
        assert client.get(f"/ui/{bad}").status_code == 404
    r = client.get("/ui/../main.py")   # the address becomes /main.py: an unknown page, so the dashboard
    assert "def " not in r.text


def test_ui_files_need_signing_in(anon):
    assert anon.get("/ui/core.js", follow_redirects=False).status_code in (302, 303, 401)


def test_version_changes_when_a_file_does(client, monkeypatch, tmp_path):
    import main
    first = re.search(r"core\.js\?v=([0-9a-f]+)", client.get("/").text).group(1)
    ui = tmp_path / "ui"
    ui.mkdir()
    for f in (main.STATIC / "ui").iterdir():
        (ui / f.name).write_bytes(f.read_bytes())
    (tmp_path / "index.html").write_text((main.STATIC / "index.html").read_text())
    (ui / "core.js").write_text((ui / "core.js").read_text() + "\n// changed\n")
    monkeypatch.setattr(main, "STATIC", tmp_path)
    assert re.search(r"core\.js\?v=([0-9a-f]+)", client.get("/").text).group(1) != first


def test_uploaded_scripts_never_served_as_scripts(client, browse):
    (browse / "media" / "evil.js").write_text("alert(1)")
    for q in ("", "&download=1"):
        r = client.get(f"/api/files/raw?path=media/evil.js{q}")
        assert r.status_code == 200 and "javascript" not in r.headers["content-type"]
        assert r.headers["x-content-type-options"] == "nosniff"
