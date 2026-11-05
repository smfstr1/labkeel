"""The Overview's tiles (Settings → display.tiles), service logos, and the warning when the data folder
isn't kept across rebuilds."""
import json

import main
from conftest import sign_in


def test_tiles_default_and_saved(client):
    assert client.get("/api/meta").json()["layout"] == {"tiles": None, "chart": "auto"}
    r = client.put("/api/settings", json={"display": {"tiles": ["power", "cpu", "storage"], "chart": "cpumem"}})
    assert r.status_code == 200, r.text
    assert client.get("/api/meta").json()["layout"] == {"tiles": ["power", "cpu", "storage"], "chart": "cpumem"}
    client.put("/api/settings", json={"display": {"tiles": None, "chart": "auto"}})


def test_tiles_checked(client):
    for bad in (["cpu", "cpu"], ["nope"], [], ["cpu", "mem", "gpu", "ctr", "net", "power", "storage"], "cpu"):
        assert client.put("/api/settings", json={"display": {"tiles": bad}}).status_code == 400, bad
    r = client.put("/api/settings", json={"display": {"chart": "pie"}})
    assert r.json()["settings"]["display"]["chart"] == "auto"


def test_tiles_change_logged(client):
    client.put("/api/settings", json={"display": {"tiles": ["cpu", "net"]}})
    assert client.get("/api/events", params={"q": "Overview's tiles"}).json()
    client.put("/api/settings", json={"display": {"tiles": None}})


def test_service_logos(client, config, monkeypatch, tmp_path):
    logos = tmp_path / "logos"
    logos.mkdir()
    (logos / "jellyfin.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
    monkeypatch.setattr(main, "logos_dir", lambda: logos)
    config(json.dumps({"services": [{"name": "Jellyfin", "url": "http://x"}, {"name": "Other"}, {"name": "Mine", "logo": "jellyfin.svg"}]}))
    items = {s["name"]: s for s in client.get("/api/services?fresh=true").json()["items"]}
    assert items["Jellyfin"]["logo"] == "/api/services/logo/jellyfin" and items["Other"]["logo"] is None
    assert items["Mine"]["logo"] == "/api/services/logo/jellyfin"
    r = client.get("/api/services/logo/jellyfin")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert "sandbox" in r.headers["content-security-policy"]       # its script can never run
    for bad in ("%2e%2e%2fconfig", "nope", "%2e%2e", "a" * 100, "JELLYFIN"):
        assert client.get(f"/api/services/logo/{bad}").status_code == 404


def test_viewers_see_logos(users, client, monkeypatch, tmp_path):
    logos = tmp_path / "logos"
    logos.mkdir()
    (logos / "x.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(main, "logos_dir", lambda: logos)
    users.create("watcher", "viewer", "a-long-viewer-password")
    assert sign_in("watcher", "a-long-viewer-password").get("/api/services/logo/x").status_code == 200


def test_data_folder_warning(monkeypatch, tmp_path):
    import containers
    monkeypatch.setattr(containers, "SELF_ID", None)
    assert main.data_not_kept() is False            # not in a container: nothing to warn about
    monkeypatch.setattr(containers, "SELF_ID", "abc")
    monkeypatch.setattr(main, "DATA_DIR", tmp_path / "nowhere-mounted")
    assert main.data_not_kept() is True
    monkeypatch.setattr(main, "DATA_DIR", __import__("pathlib").Path("/"))
    assert main.data_not_kept() is False            # "/" is always a mount point
