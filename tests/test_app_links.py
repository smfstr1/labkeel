"""Opening a service in its phone app (main.app_links): the known apps, a service's own "app", and links that
must never get through."""
import main


def test_known_apps_by_name_or_logo():
    plex = main.app_links({"name": "Plex"})
    assert plex["ios"] == "plex://" and plex["android_package"] == "com.plexapp.android"
    assert main.app_links({"name": "Plex Media Server"}) == plex
    assert main.app_links({"name": "Complex"}) is None            # a whole word only
    assert main.app_links({"name": "Home Assistant"})["android"].startswith("homeassistant://")
    assert main.app_links({"name": "Photos", "logo": "immich.png"})["ios"] == "immich://"
    assert main.app_links({"name": "Sonarr"}) is None


def test_a_service_can_set_or_turn_off_its_app():
    assert main.app_links({"name": "Plex", "app": False}) is None
    assert main.app_links({"name": "Mine", "app": "myapp://open"}) == {"ios": "myapp://open", "android": "myapp://open"}
    got = main.app_links({"name": "Mine", "app": {"android": "myapp://x", "android_package": "com.example.mine"}})
    assert got == {"android": "myapp://x", "android_package": "com.example.mine"}


def test_unsafe_links_are_dropped():
    for bad in ("javascript://alert(1)", "data://text/html,hi", "JavaScript://x", 'myapp://a"onmouseover=x',
                "not a link", "file:///etc/passwd"):
        assert main.app_links({"name": "X", "app": bad}) is None, bad
    got = main.app_links({"name": "X", "app": {"android": "myapp://x", "android_package": "com.x;S.evil=1"}})
    assert got == {"android": "myapp://x"}       # a package name that isn't one is left out


def test_owncloud_opens_its_own_app():
    oc = main.app_links({"name": "ownCloud"})
    assert oc == {"ios": "owncloud://", "android": "owncloud://", "android_package": "com.owncloud.android"}
    assert main.app_links({"name": "OwnCloud - Sam"}) == oc
    assert main.app_links({"name": "Files", "logo": "owncloud"}) == oc


def test_an_added_apps_open_button_uses_its_phone_app():
    from pathlib import Path
    js = Path(main.__file__).parent / "static" / "ui"
    assert 'data-app-open="${esc(slug)}"' in (js / "apps.js").read_text()
    assert "a[data-app-open]" in (js / "services.js").read_text()
