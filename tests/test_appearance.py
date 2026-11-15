"""Settings → Appearance: themes and the menu's order live in the browser, so these check the page's parts agree."""
import re
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "app" / "static"
CSS, CORE, HTML = (UI / "ui" / "app.css").read_text(), (UI / "ui" / "core.js").read_text(), (UI / "index.html").read_text()
LOOKS = re.findall(r'^  \["([a-z]+)", "[^"]+", "[^"]+", (null|"dark"|"light")', CORE, re.M)


def test_every_theme_offered_has_its_colours():
    names = [n for n, _ in LOOKS]
    assert names[:3] == ["auto", "dark", "light"] and len(names) == 9
    for name, base in LOOKS[3:]:
        block = re.search(r':root\[data-look="%s"\] \{(.*?)\}' % name, CSS, re.S)
        assert block, name
        for var in ("--bg", "--panel", "--panel-2", "--rule", "--text", "--muted", "--faint", "--focus", "--cpu"):
            assert f"{var}:" in block.group(1), (name, var)
    assert set(re.findall(r'data-look="([a-z]+)"\]', CSS)) == set(names[3:])     # and none left over


def test_text_can_be_read_in_every_theme():
    def lum(h):
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
        f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4   # noqa: E731
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

    def contrast(a, b):
        hi, lo = sorted((lum(a), lum(b)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    for name, _ in LOOKS[3:]:
        v = dict(re.findall(r"(--[a-z0-9-]+): (#[0-9a-f]{6});", re.search(r':root\[data-look="%s"\] \{(.*?)\}' % name, CSS, re.S).group(1)))
        assert contrast(v["--text"], v["--panel"]) >= 7, name
        assert contrast(v["--muted"], v["--panel-2"]) >= 4.5, name
        assert contrast(v["--cpu"], v["--bg"]) >= 4.5, name            # the main button: page colour on the accent


def test_the_page_has_what_the_script_fills():
    for part in ('data-page="appearance"', 'id="th-grid"', 'id="nv-list"', 'id="nv-reset"'):
        assert part in HTML, part
    assert "nav-off" in CSS and '"settings"' in CORE


def test_viewers_reach_settings_for_appearance_only():
    assert '["files", "apps"].includes(v)' in CORE                      # Settings is no longer turned away
    assert 'body.viewer .nav a[data-view="settings"]' not in CSS
    assert 'if (document.body.classList.contains("viewer")) return this.page("appearance")' in (UI / "ui" / "settings.js").read_text()


def test_the_less_used_file_actions_are_in_the_more_menu():
    for fx in ('data-fx="newfile"', 'data-fx="uploaddir"', 'id="f-size"', 'data-fx="trash"', 'data-fx="keys"'):
        tag = re.search(r"<button[^>]*%s[^>]*>" % re.escape(fx), HTML).group(0)
        assert "fx-menu" in tag, fx
    for fx in ('data-fx="upload"', 'data-fx="mkdir"'):
        assert "fx-menu" not in re.search(r"<button[^>]*%s[^>]*>" % re.escape(fx), HTML).group(0)


def test_the_containers_page_has_its_filter_menu_and_log_terminal():
    js = (UI / "ui" / "containers.js").read_text()
    for part in ('id="c-filter"', 'id="c-filter-menu"', 'id="c-fold"'):
        assert part in HTML, part
    assert 'data-cf="running"' not in HTML                              # the row of buttons is gone
    for k in ("all", "running", "stopped", "attention", "updates"):
        assert f'["{k}", ' in js, k
    for part in ("data-gact", "cg-all", "cg-fold", "lg-follow", "lg-wrap", "lg-copy", "lg-save", "lg-bottom", "term-out"):
        assert part in js, part
    for cls in (".cmenu", ".cg-body", ".term-out", ".tbtn", "@keyframes row-in"):
        assert cls in CSS, cls


def test_movement_is_off_when_the_device_asks():
    block = re.search(r"@media \(prefers-reduced-motion: reduce\) \{\s*\* \{(.*?)\}", CSS, re.S).group(1)
    assert "animation: none !important" in block and "transition: none !important" in block
