"""
LabKeel's version, read from the VERSION file at the top of the repository: the one place it's set.

Everything else reads it from there: this module (Settings, the page, the User-Agent sent to registries), the
Docker image (the file is copied in next to the app), self-update (it reads VERSION on the branch it follows)
and CI (a release tag must match it, and the published image is labelled with it).

To release: change VERSION, commit, then tag that commit v<the same version>.
"""
import re
from pathlib import Path

_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[.-]?(?:dev|a|b|rc|alpha|beta)\.?\d*)?$", re.I)


def read(text):
    """The version in a VERSION file's text, or None if it doesn't look like one."""
    v = (text or "").strip()
    return v if _SEMVER.match(v) else None


def _find():
    here = Path(__file__).resolve().parent
    for p in (here / "VERSION", here.parent / "VERSION"):  # in the image / in a git checkout
        try:
            v = read(p.read_text())
        except OSError:
            continue
        if v:
            return v
    return "unknown"


VERSION = _find()
USER_AGENT = f"LabKeel/{VERSION}"
