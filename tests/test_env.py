"""Settings from environment variables (env.py): the LABKEEL_ names, the old DASH_ names, and bad values."""
import env


def test_new_name_wins_over_old(monkeypatch):
    monkeypatch.setenv("LABKEEL_BIN_DAYS", "7")
    monkeypatch.setenv("DASH_TRASH_DAYS", "90")
    assert env.number("LABKEEL_BIN_DAYS", 30) == 7


def test_old_names_still_work_and_are_reported(monkeypatch):
    monkeypatch.delenv("LABKEEL_ALLOW_ZFS_CHANGES", raising=False)
    monkeypatch.setenv("DASH_ZFS_WRITE", "1")
    assert env.flag("LABKEEL_ALLOW_ZFS_CHANGES") is True
    assert ("DASH_ZFS_WRITE", "LABKEEL_ALLOW_ZFS_CHANGES") in env.old_names_in_use()


def test_flags_read_like_people_write_them(monkeypatch):
    for raw, want in [("1", True), ("true", True), ("Yes", True), ("ON", True), ("0", False), ("off", False), ("", False)]:
        monkeypatch.setenv("LABKEEL_HSTS", raw)
        assert env.flag("LABKEEL_HSTS") is want, raw


def test_bad_values_fall_back_to_the_default(monkeypatch, capsys):
    monkeypatch.setenv("LABKEEL_HSTS", "maybe")
    assert env.flag("LABKEEL_HSTS") is False
    monkeypatch.setenv("LABKEEL_PORT", "eighty")
    assert env.number("LABKEEL_PORT", 8787) == 8787
    monkeypatch.setenv("LABKEEL_BIN_DAYS", "-3")
    assert env.number("LABKEEL_BIN_DAYS", 30, minimum=1) == 1
    out = capsys.readouterr().out
    assert "LABKEEL_HSTS" in out and "LABKEEL_PORT" in out and "LABKEEL_BIN_DAYS" in out


def test_lists(monkeypatch):
    monkeypatch.setenv("LABKEEL_PROXY_ADMINS", " alice, bob ,,")
    assert env.names("LABKEEL_PROXY_ADMINS") == ["alice", "bob"]


def test_every_old_name_is_distinct():
    assert len(set(env.RENAMED.values())) == len(env.RENAMED)
    assert all(n.startswith("LABKEEL_") for n in env.RENAMED)


def test_every_setting_is_documented():
    from pathlib import Path
    doc = (Path(__file__).resolve().parent.parent / "docs" / "settings.md").read_text()
    for new, old in env.RENAMED.items():
        assert f"`{new}`" in doc and f"| `{old}` | `{new}` |" in doc, new
    # and every setting the code reads, renamed or not
    import re
    app = Path(__file__).resolve().parent.parent / "app"
    read = {m for f in app.glob("*.py") for m in re.findall(r'env\.(?:text|flag|number)\(\s*"(LABKEEL_[A-Z_]+)"',
                                                             f.read_text())}
    assert len(read) > 30
    assert not [n for n in sorted(read) if f"`{n}`" not in doc]
