"""The Plex token in Settings: only real-looking tokens are saved, and a refused one is explained."""
import urllib.error

import upkeep
from conftest import PASSWORD


def test_real_token_saved_and_kept_private(client):
    r = client.put("/api/settings", json={"plex": {"token": "example-plex-token"}, "password": PASSWORD})
    assert r.status_code == 200, r.text
    s = r.json()["settings"]
    assert s["plex"]["token_set"] and "token" not in s["plex"]          # never sent back to the page
    client.put("/api/settings", json={"plex": {"clear_token": True}, "password": PASSWORD})


def test_a_password_is_not_a_token(client):
    """What a browser's password manager would fill in: refused, with a reason."""
    for bad in ("My-P@ssw0rd!", "short", "has space in it 123", "x" * 70):
        r = client.put("/api/settings", json={"plex": {"token": bad}, "password": PASSWORD})
        assert r.status_code == 400 and "Plex token" in r.json()["error"], bad


def test_refused_token_explained(monkeypatch):
    def refused():
        raise urllib.error.HTTPError("http://plex", 401, "Unauthorized", {}, None)
    monkeypatch.setattr(upkeep, "plex_streams", refused)
    notes = []
    upkeep.who_is_busy({"wait_minecraft": False, "wait_plex": True}, notes)
    assert notes and "refused the saved token" in notes[0] and "Unauthorized" not in notes[0]


def test_token_box_isnt_a_password_field(client):
    """Browsers fill saved passwords into password fields; the token box isn't one."""
    html = client.get("/").text
    box = html[html.index('name="plex.token"') - 120: html.index('name="plex.token"') + 200]
    assert 'type="text"' in box and 'type="password"' not in box
