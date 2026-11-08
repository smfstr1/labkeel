"""Nothing private goes public: no keys, tokens, password hashes or personal details in what's committed, and no
hidden details (camera, place, author, software) in its images. CI also runs gitleaks over the whole history.

Only what git would publish is checked (git ls-files), so your own docker-compose.yml, data and certificates are
left alone. Outside a git checkout, the same things .gitignore leaves out are skipped."""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
IGNORED_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "venv", "node_modules",
                "config", "data", "certs", "agent-data"}
IGNORED_FILES = {"docker-compose.yml", ".env", "agent.local.yml"}

LEAKS = {
    "a private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "a GitHub token": r"\b(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})",
    "an AWS key": r"\bAKIA[0-9A-Z]{16}\b",
    "a Slack token": r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
    "a Discord webhook with its token": r"discord(app)?\.com/api/webhooks/\d+/[\w-]{20,}",
    "a Tailscale key": r"\btskey-[a-z]+-[\w-]{10,}",
    "a real tailnet name": r"\btail(?!1234\b)[0-9a-f]{4,}\.ts\.net\b",
    "a password hash": r"\$argon2(id|i|d)\$v=\d+\$m=\d+,t=\d+,p=\d+\$[A-Za-z0-9+/]{16,}",
    "an email address": r"\b[A-Za-z0-9._%+-]+@(?!example\.(?:com|org|net)\b)[A-Za-z0-9-]+\.[A-Za-z][A-Za-z.]+\b",
    "someone's home folder": r"/(?:home|Users)/(?!(?:you|alice|user)\b)[A-Za-z][\w.-]*",
}


def _published():
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True, timeout=30).stdout
        if out:
            return [ROOT / p for p in out.decode().split("\0") if p]
    except (OSError, subprocess.SubprocessError):
        pass
    return [p for p in ROOT.rglob("*") if p.is_file() and not set(p.relative_to(ROOT).parts) & IGNORED_DIRS
            and p.name not in IGNORED_FILES and not p.name.endswith((".pyc", ".bak"))]


FILES = _published()


def test_sees_the_repository():
    names = {p.relative_to(ROOT).as_posix() for p in FILES}
    assert {"README.md", "app/main.py", "LICENSE"} <= names


@pytest.mark.parametrize("what,pattern", LEAKS.items(), ids=list(LEAKS))
def test_nothing_private_is_committed(what, pattern):
    rx = re.compile(pattern)
    found = []
    for p in FILES:
        if p.name == "test_release.py" or p.suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".woff2", ".ico"):
            continue
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                found.append(f"{p.relative_to(ROOT)}:{n}")
    assert not found, f"{what} in {found}"


def test_no_private_files_are_committed():
    bad = [p.relative_to(ROOT).as_posix() for p in FILES
           if p.suffix in (".pem", ".key", ".crt", ".p12", ".pfx", ".db", ".sqlite", ".sqlite3")
           or p.name in ("users.json", "passkeys.json", "session.key", "agent.json", ".env", "docker-compose.yml")]
    assert not bad, bad


def _png_extras(data):
    i, out = 8, []
    while i + 8 <= len(data):
        n, kind = int.from_bytes(data[i:i + 4], "big"), data[i + 4:i + 8]
        if kind in (b"tEXt", b"iTXt", b"zTXt", b"eXIf", b"tIME"):
            out.append(kind.decode())
        i += 12 + n
    return out


def test_images_carry_no_hidden_details():
    """No text, EXIF (camera, place), or time stamps in PNGs, and no EXIF or XMP in JPEGs."""
    found = {}
    for p in FILES:
        data = p.read_bytes() if p.suffix.lower() in (".png", ".jpg", ".jpeg") else None
        if data is None:
            continue
        extras = _png_extras(data) if data[:8] == b"\x89PNG\r\n\x1a\n" else [
            k for k, sig in (("EXIF", b"Exif\0\0"), ("XMP", b"http://ns.adobe.com/xap/")) if sig in data[:65536]]
        if extras:
            found[p.relative_to(ROOT).as_posix()] = extras
    assert not found, found
