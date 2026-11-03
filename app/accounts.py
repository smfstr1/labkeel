"""
Accounts: who can sign in, what they may do, and their passwords (kept only as Argon2id hashes).

Where accounts come from
- data/users.json: made on the first-run set-up page, then managed on the Account page, or from the
  command line inside the container (see the bottom of this file):
      docker exec -it labkeel python accounts.py add alice --role viewer
      docker exec -it labkeel python accounts.py passwd sam        (a forgotten password)
- Optionally one admin from the environment: DASH_USER with DASH_PASS_HASH, an Argon2id hash from
  `python accounts.py hash`. It can't be changed or removed from the dashboard: change it in
  docker-compose.yml. Changing it signs that account out everywhere. (A plain-text DASH_PASS is no
  longer accepted, so a password is never stored readable in the compose file.)

Roles
- admin:  everything.
- viewer: the status pages (Overview, Containers, Storage, Services). No changes, no files, no container
  logs, no process list, no settings. security.py enforces this for every request.

Each account has a random "stamp" that's part of its session cookies' signature. A new password gets a
new stamp, so changing a password signs that account out on every other device.
"""
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

DATA_DIR = Path(os.environ.get("DASH_DATA", "/data"))
ROLES = ("admin", "viewer")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")
ID_RE = re.compile(r"^[0-9a-f]{16}$")
MIN_PASSWORD = 12

# Argon2id with the library's defaults (RFC 9106's second recommendation: 64 MiB, 3 passes, 4 lanes).
# At most two hashes run at once, so a burst of sign-in attempts can't use more than 128 MiB.
_ph = PasswordHasher()
_slots = threading.BoundedSemaphore(2)
_lock = threading.RLock()
_cache = {"sig": None, "users": []}
_dummy = {}


# --------------------------------------------------------------------------- passwords
def hash_password(password):
    with _slots:
        return _ph.hash(password)


def verify(stored_hash, password):
    """True if the password matches the hash. Never raises for a bad or missing hash."""
    if not isinstance(password, str) or not stored_hash:
        return False
    with _slots:
        try:
            return _ph.verify(stored_hash, password[:1024])
        except (VerifyMismatchError, VerificationError, InvalidHashError, ValueError):
            return False


def burn_time(password):
    """Takes as long as checking a real password, for user names that don't exist, so the time a
    sign-in takes doesn't reveal which names are real."""
    if "h" not in _dummy:
        _dummy["h"] = _ph.hash(secrets.token_urlsafe(16))
    verify(_dummy["h"], password if isinstance(password, str) else "")


def password_problem(password, name=""):
    """Why a new password isn't acceptable, or None. Length is what matters most (NIST SP 800-63B)."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters. A few unrelated words make a strong, memorable password."
    if len(password) > 1024:
        return "That password is too long (1024 characters at most)."
    if name and password.casefold() == name.casefold():
        return "The password can't be the same as the user name."
    if len(set(password)) < 5:
        return "That password repeats too few different characters."
    if password.casefold() in _COMMON:
        return "That password is on the lists of common passwords attackers try first."
    return None


_COMMON = {p.casefold() for p in (
    "password1234", "123456789012", "qwertyuiopas", "passwordpassword", "letmeinletmein", "administrator",
    "changemechangeme", "change-me-to-a-long-password", "correcthorsebatterystaple", "iloveyouiloveyou",
    "homelabhomelab", "1q2w3e4r5t6y", "qwerty123456", "password123!", "welcome12345", "adminadmin123",
)}


# --------------------------------------------------------------------------- the environment account
def _env():
    """The admin set in the environment, or None. Read once."""
    if "env" in _dummy:
        return _dummy["env"]
    name = (os.environ.get("DASH_USER") or "").strip()
    phash = (os.environ.get("DASH_PASS_HASH") or "").strip()
    acct = None
    if os.environ.get("DASH_PASS"):
        print("DASH_PASS is no longer used: remove it from docker-compose.yml (it holds a password in plain text). "
              "Use DASH_PASS_HASH instead (make one with: python accounts.py hash), or an account from the set-up page.",
              flush=True)
    if phash and not phash.startswith("$argon2"):
        print("DASH_PASS_HASH isn't an Argon2 hash (make one with: python accounts.py hash); ignoring it.", flush=True)
        phash = ""
    if name and phash:
        acct = {
            "id": hashlib.sha256(f"env:{name}".encode()).hexdigest()[:16],
            "name": name, "role": "admin", "source": "env", "hash": phash,
            # part of the session signature, so a new hash in docker-compose.yml signs this account out everywhere
            "stamp": hashlib.sha256(f"env|{name}|{phash}".encode()).hexdigest()[:32],
        }
    elif name:
        print("DASH_USER is set without DASH_PASS_HASH, so it isn't an account. Add DASH_PASS_HASH, or remove DASH_USER.",
              flush=True)
    _dummy["env"] = acct
    return acct


# --------------------------------------------------------------------------- data/users.json
def _path():
    return DATA_DIR / "users.json"


def _load_file():
    p = _path()
    try:
        st = p.stat()
        sig = (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        sig = None
    with _lock:
        if sig != _cache["sig"]:
            users = []
            if sig:
                try:
                    data = json.loads(p.read_text())
                    for u in data.get("users", []) if isinstance(data, dict) else []:
                        if (isinstance(u, dict) and ID_RE.match(str(u.get("id", ""))) and NAME_RE.match(str(u.get("name", "")))
                                and u.get("role") in ROLES and str(u.get("hash", "")).startswith("$argon2")):
                            users.append({**u, "source": "local"})
                except (OSError, ValueError) as e:
                    print(f"Couldn't read {p}: {e}. Nobody from it can sign in until it's fixed.", flush=True)
            _cache.update(sig=sig, users=users)
        return _cache["users"]


def _save_file(users):
    """Write users.json readable by its owner only, swapped in whole and flushed to disk."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    p = _path()
    tmp = p.with_name(f"users.json.{os.getpid()}.{threading.get_ident()}.tmp")
    clean = [{k: v for k, v in u.items() if k != "source"} for u in users]
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({"version": 1, "users": clean}, indent=2))
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    tmp.replace(p)
    _cache["sig"] = None  # read afresh next time


# --------------------------------------------------------------------------- looking accounts up
def all_users():
    """Every account: the environment's admin first (if any), then users.json. A users.json account with
    the same name as the environment's is ignored."""
    env = _env()
    file_users = _load_file()
    if env:
        return [env] + [u for u in file_users if u["name"].casefold() != env["name"].casefold()]
    return list(file_users)


def exist():
    return bool(all_users())


def by_name(name):
    if not isinstance(name, str) or not name:
        return None
    n = name.strip().casefold()
    return next((u for u in all_users() if u["name"].casefold() == n), None)


def by_id(uid):
    if not isinstance(uid, str) or not ID_RE.match(uid):
        return None
    return next((u for u in all_users() if u["id"] == uid), None)


def admins():
    return [u for u in all_users() if u["role"] == "admin"]


def public(u):
    """What may be shown about an account (never its hash or stamp)."""
    return {"id": u["id"], "name": u["name"], "role": u["role"], "source": u.get("source", "local"),
            "created": u.get("created"), "changed": u.get("changed")}


# --------------------------------------------------------------------------- changing accounts
class Problem(Exception):
    """A change that isn't allowed, with a message for the person making it."""


def create(name, role, password):
    name = (name or "").strip() if isinstance(name, str) else ""
    if not NAME_RE.match(name):
        raise Problem("A user name is 1 to 64 letters, numbers, dots, dashes, underscores or @, starting with a letter or number.")
    if role not in ROLES:
        raise Problem("The role must be admin or viewer.")
    why = password_problem(password, name)
    if why:
        raise Problem(why)
    h = hash_password(password)
    with _lock:
        if by_name(name):
            raise Problem(f"There's already an account called {name}.")
        users = list(_load_file())
        now = time.time()
        u = {"id": secrets.token_hex(8), "name": name, "role": role, "hash": h,
             "stamp": secrets.token_hex(16), "created": now, "changed": now}
        _save_file(users + [u])
    return {**u, "source": "local"}


def _change(uid, fn):
    with _lock:
        users = [dict(u) for u in _load_file()]
        u = next((x for x in users if x["id"] == uid), None)
        if u is None:
            env = _env()
            if env and env["id"] == uid:
                raise Problem("This account is set in docker-compose.yml (DASH_USER), so change it there.")
            raise Problem("There's no such account.")
        fn(u, users)
        _save_file(users)
        return {**u, "source": "local"}


def set_password(uid, password, keep_stamp=False):
    """A new password. Unless keep_stamp (a rehash at sign-in), every session of that account ends."""
    u = by_id(uid)
    why = password_problem(password, u["name"] if u else "")
    if why:
        raise Problem(why)
    h = hash_password(password)

    def apply(u, users):
        u["hash"] = h
        u["changed"] = time.time()
        if not keep_stamp:
            u["stamp"] = secrets.token_hex(16)
    return _change(uid, apply)


def rehash_if_needed(u, password):
    """After a correct password: upgrade the stored hash if the hashing settings have got stronger."""
    if u.get("source") != "local":
        return
    try:
        if _ph.check_needs_rehash(u["hash"]):
            h = hash_password(password)
            _change(u["id"], lambda x, _: x.update(hash=h))
    except Exception:
        pass


def _admins_after(users, removed=None, demoted=None):
    n = sum(1 for x in users if x["role"] == "admin" and x["id"] not in (removed, demoted))
    return n + (1 if _env() else 0)


def set_role(uid, role):
    if role not in ROLES:
        raise Problem("The role must be admin or viewer.")

    def apply(u, users):
        if role != "admin" and _admins_after(users, demoted=uid) == 0:
            raise Problem("That's the last admin. Make someone else an admin first.")
        if u["role"] != role:
            u["role"] = role
            u["stamp"] = secrets.token_hex(16)  # signed in again, with the new role
    return _change(uid, apply)


def remove(uid):
    with _lock:
        users = list(_load_file())
        u = next((x for x in users if x["id"] == uid), None)
        if u is None:
            env = _env()
            if env and env["id"] == uid:
                raise Problem("This account is set in docker-compose.yml (DASH_USER), so remove it there.")
            raise Problem("There's no such account.")
        if u["role"] == "admin" and _admins_after(users, removed=uid) == 0:
            raise Problem("That's the last admin. Make someone else an admin first.")
        _save_file([x for x in users if x["id"] != uid])
        return u


# --------------------------------------------------------------------------- first-run set-up
_setup = {"code": None}
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I to misread


def setup_code():
    """The one-time code the set-up page asks for, shown in the container's log, so only someone who can
    read the log (who runs the server) can create the first admin. None once an account exists."""
    if exist():
        return None
    with _lock:
        if not _setup["code"]:
            raw = "".join(secrets.choice(_ALPHABET) for _ in range(12))
            _setup["code"] = f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"
        return _setup["code"]


def setup_code_ok(code):
    real = setup_code()
    if not real or not isinstance(code, str):
        return False
    given = code.strip().upper().replace(" ", "")
    return hmac.compare_digest(given.encode(), real.encode())


# --------------------------------------------------------------------------- command line
def _ask_password(name):
    while True:
        pw = getpass.getpass(f"New password for {name}: ")
        why = password_problem(pw, name)
        if why:
            print(why)
            continue
        if getpass.getpass("Again: ") != pw:
            print("Those don't match. Try again.")
            continue
        return pw


def _cli(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="python accounts.py", description="Manage dashboard accounts (data/users.json).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show every account")
    a = sub.add_parser("add", help="add an account")
    a.add_argument("name")
    a.add_argument("--role", choices=ROLES, default="viewer")
    p = sub.add_parser("passwd", help="set a new password (also for a forgotten one)")
    p.add_argument("name")
    r = sub.add_parser("role", help="change an account's role")
    r.add_argument("name")
    r.add_argument("role", choices=ROLES)
    d = sub.add_parser("remove", help="remove an account")
    d.add_argument("name")
    sub.add_parser("hash", help="print an Argon2id hash of a password, for DASH_PASS_HASH")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "hash":
            print(hash_password(_ask_password("DASH_PASS_HASH")))
        elif args.cmd == "list":
            for u in all_users():
                print(f"{u['name']:24} {u['role']:7} {'(from docker-compose.yml)' if u.get('source') == 'env' else ''}")
            if not exist():
                print("No accounts yet. Add one with: python accounts.py add <name> --role admin")
        elif args.cmd == "add":
            create(args.name, args.role, _ask_password(args.name))
            print(f"Added {args.name} ({args.role}).")
        else:
            u = by_name(args.name)
            if not u:
                raise Problem(f"There's no account called {args.name}.")
            if args.cmd == "passwd":
                set_password(u["id"], _ask_password(u["name"]))
                print(f"New password set for {u['name']}. They're signed out everywhere.")
            elif args.cmd == "role":
                set_role(u["id"], args.role)
                print(f"{u['name']} is now {args.role}.")
            elif args.cmd == "remove":
                remove(u["id"])
                print(f"Removed {u['name']}.")
    except Problem as e:
        print(e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
