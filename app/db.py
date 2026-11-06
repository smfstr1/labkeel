"""
LabKeel's database: one SQLite file, data/labkeel.db, for records that grow over time and need searching,
such as the activity log and image update checks.

Why SQLite: it's a single file in the data folder (backed up with everything else), needs no server, and
copes comfortably with a homelab's write rate. Settings stay in small JSON files, which are easy to read and
fix by hand. The history charts keep their own file (history.db, see metrics.py), because they write every
few seconds and prune themselves on a different schedule.

How it's kept safe and fast:
- WAL journal mode: readers never wait for a writer, and a crash mid-write can't corrupt the file.
- synchronous=NORMAL with WAL: durable across application crashes; at worst the last moment's records are
  lost if the whole machine loses power.
- Foreign keys enforced, and a 5 s busy timeout so two writers queue rather than fail.
- One connection per thread (SQLite connections can't be shared between threads safely).

Schema changes are migrations: numbered steps in MIGRATIONS, applied in order, each in its own transaction.
The number reached is stored in the file itself (PRAGMA user_version), so an older database is brought up
to date on start-up and a newer one (from a later LabKeel) is refused rather than misread.
"""
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import env

DATA_DIR = Path(env.text("LABKEEL_DATA_DIR", "/data"))
_local = threading.local()
_init_lock = threading.Lock()
_ready = {"path": None}


class DatabaseError(Exception):
    """The database can't be used (damaged, or from a newer LabKeel). The message says what to do."""


def path():
    return DATA_DIR / "labkeel.db"


# --------------------------------------------------------------------------- migrations
def _run(con, script):
    """Each statement of a migration in turn, inside the caller's transaction. (sqlite3's executescript would
    commit the transaction first, so a failing migration could be left half done.)"""
    for stmt in script.split(";"):
        lines = [ln.split("--")[0] for ln in stmt.splitlines()]
        sql = "\n".join(lines).strip()
        if sql:
            con.execute(sql)


def _m1_events(con):
    """The activity log: what happened, when, and (for things people did) who and from where. Imports the
    old events.json once, then renames it so it's kept but not imported again."""
    _run(con, """
        CREATE TABLE events (
            id    INTEGER PRIMARY KEY,
            t     REAL    NOT NULL,             -- seconds since 1970 (UTC)
            kind  TEXT    NOT NULL,             -- alert, error, settings, container, zfs, drive, files, ...
            text  TEXT    NOT NULL,
            user  TEXT,                         -- who did it, if a person did
            ip    TEXT                          -- and from which address
        );
        CREATE INDEX events_t ON events (t);
        CREATE INDEX events_kind_t ON events (kind, t);
    """)
    old = DATA_DIR / "events.json"
    try:
        rows = json.loads(old.read_text())
    except (OSError, ValueError):
        return
    con.executemany("INSERT INTO events (t, kind, text) VALUES (?, ?, ?)",
                    [(float(e["t"]), str(e.get("kind") or "settings"), str(e.get("text") or ""))
                     for e in rows if isinstance(e, dict) and isinstance(e.get("t"), (int, float))])
    try:
        old.replace(old.with_name("events.json.imported"))
    except OSError:
        pass


def _m2_image_checks(con):
    """The latest update check for each image a container uses (see updates.py)."""
    _run(con, """
        CREATE TABLE image_checks (
            image         TEXT PRIMARY KEY,     -- as the container names it: "jellyfin/jellyfin:latest"
            status        TEXT NOT NULL CHECK (status IN ('current', 'update', 'pinned', 'local', 'unknown', 'error')),
            local_digest  TEXT,
            remote_digest TEXT,
            detail        TEXT,                 -- why it couldn't be checked, in words
            checked       REAL NOT NULL
        );
    """)


MIGRATIONS = [_m1_events, _m2_image_checks]   # never reorder or edit a released step: add a new one


def _migrate(con):
    have = con.execute("PRAGMA user_version").fetchone()[0]
    if have > len(MIGRATIONS):
        raise DatabaseError(f"{path()} was made by a newer version of LabKeel (schema {have}; this one knows "
                            f"{len(MIGRATIONS)}). Update LabKeel, or restore an older copy of the data folder.")
    for n, step in enumerate(MIGRATIONS[have:], start=have + 1):
        con.execute("BEGIN IMMEDIATE")
        try:
            step(con)
            con.execute(f"PRAGMA user_version = {n}")
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise


# --------------------------------------------------------------------------- connections
def _open():
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        con = sqlite3.connect(str(p), timeout=5, isolation_level=None, check_same_thread=True)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout = 5000")
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.DatabaseError as e:
        raise DatabaseError(f"Can't open {p}: {e}. If it's damaged, move it aside and LabKeel starts a new one.") from e
    with _init_lock:
        if _ready["path"] != str(p):
            # WAL is stored in the file, so it's set once, here, before anything else uses the database.
            # (Setting it on every new connection can fail with "database is locked" while another one writes.)
            if con.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                con.execute("PRAGMA journal_mode = WAL")
            _migrate(con)
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass
            _ready["path"] = str(p)
    return con


def connection():
    """This thread's connection, opened (and the schema brought up to date) on first use."""
    con = getattr(_local, "con", None)
    if con is None or getattr(_local, "path", None) != str(path()):
        con = _open()
        _local.con, _local.path = con, str(path())
    return con


@contextmanager
def transaction():
    """Several writes that must all happen or none:  with db.transaction() as con: con.execute(...)"""
    con = connection()
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def query(sql, params=()):
    """Rows as dicts."""
    return [dict(r) for r in connection().execute(sql, params).fetchall()]


def execute(sql, params=()):
    """One write, committed straight away. Returns the new row's id, if it made one."""
    return connection().execute(sql, params).lastrowid


def reset_for_tests():
    """Forget this thread's connection and the migration flag (tests point DATA_DIR somewhere new)."""
    con = getattr(_local, "con", None)
    if con is not None:
        con.close()
    _local.con = None
    _ready["path"] = None


# --------------------------------------------------------------------------- the activity log
EVENT_DAYS = 365       # entries older than this are removed
EVENT_MAX = 50_000     # and never more than this many
_pruned = {"t": 0.0}


def add_event(kind, text, user=None, ip=None, t=None):
    execute("INSERT INTO events (t, kind, text, user, ip) VALUES (?, ?, ?, ?, ?)",
            (t if t is not None else time.time(), str(kind)[:32], str(text)[:2000],
             (str(user)[:128] if user else None), (str(ip)[:64] if ip else None)))
    now = time.time()
    if now - _pruned["t"] > 3600:  # tidy up at most once an hour
        _pruned["t"] = now
        with transaction() as con:
            con.execute("DELETE FROM events WHERE t < ?", (now - EVENT_DAYS * 86400,))
            con.execute("DELETE FROM events WHERE id <= (SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?)",
                        (EVENT_MAX,))


def events(limit=100, kind=None, q=None, before=None):
    """Newest first. `before` (an id) pages back through older ones; `q` searches the text and who did it."""
    where, params = [], []
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(text LIKE ? ESCAPE '\\' OR user LIKE ? ESCAPE '\\')")
        params += [like, like]
    if before:
        where.append("id < ?")
        params.append(int(before))
    sql = "SELECT id, t, kind, text, user, ip FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    return query(sql, params)
