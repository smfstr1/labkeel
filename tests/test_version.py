"""The version is set in one place, the VERSION file, and everything reads it from there."""
from pathlib import Path

import pytest

import version

ROOT = Path(__file__).resolve().parent.parent


def test_the_app_reads_the_version_file():
    assert version.VERSION == (ROOT / "VERSION").read_text().strip()
    assert version.USER_AGENT == f"LabKeel/{version.VERSION}"


@pytest.mark.parametrize("text, want", [("0.1.0\n", "0.1.0"), ("1.2.3.dev0", "1.2.3.dev0"), ("2.0.0rc1", "2.0.0rc1"),
                                        ("v1.0.0", None), ("", None), ("1.0", None), ("1.0.0; rm -rf /", None)])
def test_reading_a_version(text, want):
    assert version.read(text) == want


def test_nothing_else_sets_its_own_version():
    """No second copy of the number to forget when releasing."""
    v = version.VERSION
    for p in [*ROOT.glob("app/*.py"), ROOT / "Dockerfile", ROOT / "docker-compose.example.yml"]:
        assert v not in p.read_text(), f"{p.name} repeats the version; read it from VERSION instead"
