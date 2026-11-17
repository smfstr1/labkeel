"""Settings → Appearance: themes and the menu's order live in the browser, so these check the page's parts agree."""
import re
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "app" / "static"
CSS, CORE, HTML = (UI / "ui" / "app.css").read_text(), (UI / "ui" / "core.js").read_text(), (UI / "index.html").read_text()
LOOKS = re.findall(r'^  \["([a-z]+)", "[^"]+", "[^"]+", (null|"dark"|"light")', CORE, re.M)


def test_every_theme_offered_has_its_colours():
    names = [n for n, _ in LOOKS]
    assert names[:3] == ["auto", "dark", "light"] and len(names) == 15
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


def test_your_own_touches_are_all_there():
    js = (UI / "ui" / "settings.js").read_text()
    for key in ("accent", "width", "font", "icons", "motion"):
        assert f"  {key}: " in CORE and f'"{key}"' in js, key
    assert "max-width: var(--page-max, none)" in CSS                    # pages fill the screen unless you hold them
    assert ':root[data-motion="off"] *' in CSS and ":root[data-navicons]" in CSS
    for part in ('id="ap-rows"', 'id="ap-reset"'):
        assert part in HTML, part
    # every accent can be read as text on the lightest and darkest panels it lands on
    def lum(h):
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
        f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4   # noqa: E731
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

    def contrast(a, b):
        hi, lo = sorted((lum(a), lum(b)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)
    accents = re.findall(r'^  \["[a-z]+", "[A-Za-z]+", "(#[0-9a-f]{6})", "#[0-9a-f]{6}", "(#[0-9a-f]{6})", "#[0-9a-f]{6}"\],$', CORE, re.M)
    assert len(accents) == 7
    for dark, light in accents:
        assert contrast(dark, "#2e231d") >= 4.5 and contrast(light, "#f4efe6") >= 4.5, (dark, light)


def test_an_apps_totals_sit_under_the_cpu_and_memory_headings():
    js = (UI / "ui" / "containers.js").read_text()
    assert 'class="cg-num cg-cpu"' in js and 'class="cg-num cg-mem"' in js and 'class="cg-lead"' in js
    assert CSS.count("grid-template-columns: var(--ccols);") == 2   # the rows and the app headings share one set of columns


def test_a_pools_datasets_and_snapshots_read_in_full_on_a_phone():
    js = (UI / "ui" / "zfs.js").read_text()
    assert 'class="dstags"' in js and 'style="--depth:${depth}"' in js
    phone = CSS.split("a phone: each dataset is a small card", 1)[1].split("@media", 1)[0]
    for rule in (".dsrow .dstags {", ".dsrow.item > .mp {", ".snrow .nm,", ".dd .fields > .field {", "overflow-wrap: anywhere;"):
        assert rule in phone, rule


def test_overview_cards_and_wording():
    html, ov = (UI / "index.html").read_text(), (UI / "ui" / "overview.js").read_text()
    assert 'data-tile="net" data-open="net"' in html and "NetView.open()" in ov      # Network opens its traffic history
    assert "Measured:" not in ov and "pwHist" not in ov                          # the Power line comes from the server
    assert "h.pw" in ov and "pw: h.pw" in (UI / "ui" / "core.js").read_text()
    assert "Containers are grouped by" not in html and "ZFS keeps recently" not in html
    assert ">Non-RAID drives</h2>" in html and "Other drives" not in (UI / "ui" / "storage.js").read_text()
