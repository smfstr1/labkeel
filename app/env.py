"""
LabKeel's own settings from environment variables: the `environment:` lines in docker-compose.yml.

Every name starts with LABKEEL_ and says what it does. Before 0.1.2 they started with DASH_ (and some were named
differently); the old names still work, and LabKeel lists any it finds in its log with their new names.

On/off settings take 1, true, yes or on (and 0, false, no or off). A value that can't be read is reported in
the log and the default is used instead, so a typo never stops LabKeel from starting.
"""
import os

# Every setting, with its old name. INSTALL.md describes what each one does.
RENAMED = {
    # what LabKeel may change on the machine (all off unless turned on)
    "LABKEEL_ALLOW_FILE_CHANGES": "DASH_FILES_WRITE",
    "LABKEEL_ALLOW_ZFS_CHANGES": "DASH_ZFS_WRITE",
    "LABKEEL_ALLOW_RESTART": "DASH_ALLOW_REBOOT",
    # sign-in
    "LABKEEL_ADMIN_USER": "DASH_USER",
    "LABKEEL_ADMIN_PASSWORD_HASH": "DASH_PASS_HASH",
    "LABKEEL_ALLOW_NO_SIGN_IN": "DASH_ALLOW_NO_AUTH",
    "LABKEEL_HOSTNAMES": "DASH_HOSTNAMES",
    # HTTPS and the network
    "LABKEEL_PORT": "DASH_PORT",
    "LABKEEL_LISTEN_ADDRESS": "DASH_HOST",
    "LABKEEL_HTTPS": "DASH_TLS",
    "LABKEEL_HSTS": "DASH_HSTS",
    "LABKEEL_MAX_CONNECTIONS": "DASH_MAX_CONNECTIONS",
    # behind a reverse proxy
    "LABKEEL_TRUSTED_PROXY": "DASH_TRUSTED_PROXY",
    "LABKEEL_PROXY_USER_HEADER": "DASH_AUTH_HEADER",
    "LABKEEL_PROXY_ADMINS": "DASH_PROXY_ADMINS",
    "LABKEEL_PROXY_VIEWERS": "DASH_PROXY_VIEWERS",
    # files
    "LABKEEL_UPLOAD_LIMIT_GB": "DASH_UPLOAD_MAX_GB",
    "LABKEEL_BIN_DAYS": "DASH_TRASH_DAYS",
    # where things are inside the container (the example compose file's folders match these)
    "LABKEEL_DATA_DIR": "DASH_DATA",
    "LABKEEL_CONFIG_FILE": "DASH_CONFIG",
    "LABKEEL_CERTS_DIR": "DASH_CERTS",
    "LABKEEL_FILES_DIR": "DASH_BROWSE_ROOT",
    # rarely needed
    "LABKEEL_HOST_COMMANDS": "DASH_HOST_NS",
    "LABKEEL_LOG_LEVEL": "DASH_LOG_LEVEL",
    "LABKEEL_DRIVE_CHECK_SECONDS": "DASH_SMART_INTERVAL",
    "LABKEEL_POOL_CHECK_SECONDS": "DASH_DRIVECARE_TICK",
    "LABKEEL_POOL_FULL_CHECK_SECONDS": "DASH_DRIVECARE_FULL",
    "LABKEEL_ZFS_CHECK_SECONDS": "DASH_ZFS_TICK",
    "LABKEEL_POWERCAP_DIR": "DASH_POWERCAP",
}
_YES = ("1", "true", "yes", "on")
_NO = ("0", "false", "no", "off")


def _say(text):
    print(f"Setting {text}", flush=True)


def text(name, default=""):
    """A setting as text: from its LABKEEL_ name, else its old name, else `default`."""
    for key in (name, RENAMED.get(name)):
        if key and key in os.environ:
            return os.environ[key]
    return default


def flag(name, default=False):
    """An on/off setting."""
    raw = text(name).strip().lower()
    if not raw:
        return default
    if raw in _YES:
        return True
    if raw in _NO:
        return False
    _say(f"{name}={raw!r} isn't on or off (1 or 0), so it's {'on' if default else 'off'}.")
    return default


def number(name, default, minimum=None):
    """A number (whole if the default is whole)."""
    raw = text(name).strip()
    if not raw:
        return default
    try:
        value = int(raw) if isinstance(default, int) else float(raw)
    except ValueError:
        _say(f"{name}={raw!r} isn't a number, so it's {default}.")
        return default
    if minimum is not None and value < minimum:
        _say(f"{name}={raw} is below {minimum}, so it's {minimum}.")
        return minimum
    return value


def names(name):
    """A comma-separated list: 'alice, bob' → ['alice', 'bob']."""
    return [part.strip() for part in text(name).split(",") if part.strip()]


def old_names_in_use():
    """[(old name, new name)] for settings still given by their old names."""
    return [(old, new) for new, old in RENAMED.items() if old in os.environ and new not in os.environ]
