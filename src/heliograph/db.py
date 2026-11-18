"""The local store: one SQLite database for the library and a second one for thumbnails.

Design goals: private (0700 dir, 0600 files), fast on millions of rows, and hard to lose.
  * WAL journal, NORMAL sync, per-thread connections, `PRAGMA user_version` migrations.
  * Everything that can be rebuilt by re-scanning (files, stats, probes) is separate in intent from
    what cannot (library folders, favourites, settings); the latter is mirrored to a tiny JSON
    snapshot so a corrupt database can be replaced without losing the user's choices.
  * The thumbnail cache lives in its own file (thumbs.py) so pruning or deleting it never touches
    the library.
"""

import json
import os
import sqlite3
import threading
import time

from .paths import paths

SCHEMA_VERSION = 1

MIGRATIONS = {
    1: """
CREATE TABLE roots(
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  persist INTEGER NOT NULL DEFAULT 1,          -- 1 = in the library, 0 = opened for this session only
  depth INTEGER NOT NULL DEFAULT -1,           -- -1 = whole tree, 0 = only the folder itself
  added INTEGER NOT NULL,
  last_scan INTEGER NOT NULL DEFAULT 0);

CREATE TABLE files(
  id INTEGER PRIMARY KEY,
  root_id INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
  lib INTEGER NOT NULL DEFAULT 1,              -- 1 when owned by a library root (copied from the root)
  path TEXT NOT NULL UNIQUE,
  dir TEXT NOT NULL, name TEXT NOT NULL, ext TEXT NOT NULL, kind TEXT NOT NULL,
  size INTEGER NOT NULL, mtime INTEGER NOT NULL,
  ts INTEGER NOT NULL,                         -- date shown and grouped by: taken date if known, else mtime
  taken INTEGER,
  w INTEGER, h INTEGER, dur REAL, title TEXT, artist TEXT, album TEXT, codec TEXT,
  name_key TEXT NOT NULL, dir_key TEXT NOT NULL, search TEXT NOT NULL,
  shot INTEGER NOT NULL DEFAULT 0,
  meta INTEGER NOT NULL DEFAULT 0,             -- 0 = details not read yet, 1 = read, 2 = unreadable
  cost INTEGER NOT NULL DEFAULT 1,             -- 1 = details come from a cheap header read, 2 = need ffprobe
  seen INTEGER NOT NULL DEFAULT 0,             -- scan generation that last saw the file
  added INTEGER NOT NULL);
CREATE INDEX files_root ON files(root_id, seen);
CREATE INDEX files_dir ON files(dir);
-- The two orderings every session uses (folder view newest-first, and date order, which also serves the
-- month grouping and date ranges): covering indexes, so building a view of a million files needs no sort.
CREATE INDEX files_ord_folder ON files(lib, dir_key, dir, ts DESC, name_key, id);
CREATE INDEX files_ord_date ON files(lib, ts DESC, name_key, id);
CREATE INDEX files_lib_kind_ts ON files(lib, kind, ts);
CREATE INDEX files_todo ON files(meta, cost, id) WHERE meta=0;
CREATE INDEX files_shot ON files(shot) WHERE shot=1;

CREATE TABLE dirstats(
  dir TEXT PRIMARY KEY, lib INTEGER NOT NULL,
  n_image INTEGER NOT NULL, n_video INTEGER NOT NULL, n_audio INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE monthstats(ym TEXT PRIMARY KEY, n INTEGER NOT NULL) WITHOUT ROWID;

CREATE TABLE favourites(path TEXT PRIMARY KEY, added INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE settings(k TEXT PRIMARY KEY, v TEXT NOT NULL) WITHOUT ROWID;

CREATE TABLE probes(                           -- full ffprobe result of files that were opened
  file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  mtime INTEGER NOT NULL, size INTEGER NOT NULL, json TEXT NOT NULL);

CREATE TABLE jobs(                             -- conversions
  id INTEGER PRIMARY KEY,
  created INTEGER NOT NULL, started INTEGER, finished INTEGER,
  status TEXT NOT NULL,                        -- queued | running | done | failed | cancelled
  kind TEXT NOT NULL, preset TEXT NOT NULL, params TEXT NOT NULL,
  src TEXT NOT NULL, dst TEXT,
  progress REAL NOT NULL DEFAULT 0, message TEXT,
  bytes_in INTEGER, bytes_out INTEGER);
CREATE INDEX jobs_status ON jobs(status, id);
""",
}

_tls = threading.local()
_snapshot_lock = threading.Lock()
_snapshot_timer = None


def _configure(c):
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA temp_store=FILE")             # big sorts go to disk, not RAM
    c.execute("PRAGMA cache_size=-8000")            # 8 MB page cache per connection
    c.execute("PRAGMA mmap_size=0")
    c.execute("PRAGMA trusted_schema=OFF")
    return c


def connect(path=None, readonly=False):
    """A new connection (autocommit; writers open explicit transactions)."""
    p = path or paths().db
    if readonly:
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30, isolation_level=None,
                            check_same_thread=False)
    else:
        c = sqlite3.connect(p, timeout=30, isolation_level=None, check_same_thread=False)
    return _configure(c)


def conn():
    """The calling thread's connection to the library database."""
    c = getattr(_tls, "conn", None)
    p = str(paths().db)
    if c is None or getattr(_tls, "path", None) != p:
        c = connect()
        _tls.conn, _tls.path = c, p
    return c


def close_thread_connection():
    c = getattr(_tls, "conn", None)
    if c is not None:
        try:
            c.close()
        except sqlite3.Error:
            pass
        _tls.conn = None


def migrate(c):
    cur = c.execute("PRAGMA user_version").fetchone()[0]
    if cur > SCHEMA_VERSION:
        raise RuntimeError(f"database is from a newer Heliograph (schema {cur} > {SCHEMA_VERSION})")
    for v in range(cur + 1, SCHEMA_VERSION + 1):
        c.execute("BEGIN IMMEDIATE")
        try:
            for stmt in _split(MIGRATIONS[v]):
                c.execute(stmt)
            c.execute(f"PRAGMA user_version={v}")
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def _split(script):
    """Split a migration script into statements (no semicolons inside our statements)."""
    out = []
    for part in script.split(";"):
        lines = [ln for ln in part.splitlines() if not ln.strip().startswith("--")]
        text = "\n".join(lines).strip()
        if text:
            out.append(text)
    return out


def init(check=False):
    """Create/upgrade the database. With check=True a quick integrity check runs first; a corrupt
    database is moved aside and rebuilt from the snapshot of user data. Returns a status string."""
    p = paths()
    p.ensure()
    status = "ok"
    if check and p.db.exists():
        c = sqlite3.connect(p.db, timeout=30)
        try:
            res = c.execute("PRAGMA quick_check").fetchone()[0]
        except sqlite3.DatabaseError as e:
            res = str(e)
        finally:
            c.close()
        if res != "ok":
            status = recover(res)
    c = sqlite3.connect(p.db, timeout=30, isolation_level=None)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        migrate(_configure(c))
    finally:
        c.close()
    try:
        os.chmod(p.db, 0o600)
    except OSError:
        pass
    if status.startswith("recovered"):
        restore_user_data()
    return status


def recover(reason):
    """Move a damaged database aside; the next init() creates a fresh one."""
    p = paths()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for suffix in ("", "-wal", "-shm"):
        src = p.db.with_name(p.db.name + suffix)
        if src.exists():
            os.replace(src, p.db.with_name(f"{p.db.name}.corrupt-{stamp}{suffix}"))
    return f"recovered ({reason[:80]})"


# --------------------------------------------------------------------------- settings

# key -> (default, allowed values, what it does). The one place that defines settings: the API, the
# `heliograph config` command and the docs all follow it.
SETTINGS = {
    "bg_index": ("auto", ("auto", "paused"),
                 "Reading file details in the background: auto (when the PC is calm) or paused"),
    "thumb_cache_mb": ("2048", ("256", "512", "1024", "2048", "4096", "8192"),
                       "Size limit of the thumbnail cache in MB (least recently used are removed)"),
    "transcode_gpu": ("auto", ("auto", "off"),
                      "Live video conversion for playback: use the GPU (VAAPI) when there is one, or never"),
    "transcode_max_height": ("1080", ("2160", "1440", "1080", "720", "480"),
                             "Largest picture height for live video conversion; lower is easier on a slow CPU"),
    "idle_exit_min": ("15", ("5", "15", "30", "60", "240"),
                      "Minutes without use before the background service exits (applies at its next start)"),
}
DEFAULT_SETTINGS = {k: v[0] for k, v in SETTINGS.items()}


def validate_setting(key, value):
    """The value as stored (a string), or ValueError with a message that can be shown to the user."""
    if key not in SETTINGS:
        raise ValueError(f"unknown setting {key!r}; known: {', '.join(SETTINGS)}")
    value = str(value)
    if value not in SETTINGS[key][1]:
        raise ValueError(f"{key} must be one of: {', '.join(SETTINGS[key][1])}")
    return value


def get_setting(key, default=None):
    r = conn().execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    if r:
        return r[0]
    return DEFAULT_SETTINGS.get(key, default)


def set_setting(key, value):
    conn().execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                   (key, str(value)))
    schedule_snapshot()


def all_settings():
    out = dict(DEFAULT_SETTINGS)
    for r in conn().execute("SELECT k, v FROM settings"):
        out[r[0]] = r[1]
    return out


# --------------------------------------------------------------------------- user-data snapshot

def schedule_snapshot(delay=2.0):
    """Write the snapshot of user data shortly after a change (debounced)."""
    global _snapshot_timer
    with _snapshot_lock:
        if _snapshot_timer is not None:
            _snapshot_timer.cancel()
        _snapshot_timer = threading.Timer(delay, write_user_data)
        _snapshot_timer.daemon = True
        _snapshot_timer.start()


def write_user_data():
    p = paths()
    try:
        c = connect(readonly=True)
        try:
            data = {
                "format": 1, "saved": int(time.time()),
                "roots": [dict(r) for r in c.execute(
                    "SELECT path, depth, added FROM roots WHERE persist=1 ORDER BY path")],
                "favourites": [dict(r) for r in c.execute("SELECT path, added FROM favourites ORDER BY path")],
                "settings": {r[0]: r[1] for r in c.execute("SELECT k, v FROM settings")}}
        finally:
            c.close()
        tmp = p.user_json.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p.user_json)
    except (OSError, sqlite3.Error):
        pass


def restore_user_data():
    """Re-create roots, favourites and settings from the snapshot into a fresh database."""
    p = paths()
    try:
        data = json.loads(p.user_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    c = connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        now = int(time.time())
        for r in data.get("roots", []):
            c.execute("INSERT OR IGNORE INTO roots(path, persist, depth, added) VALUES(?,1,?,?)",
                      (r["path"], int(r.get("depth", -1)), int(r.get("added", now))))
        for f in data.get("favourites", []):
            c.execute("INSERT OR IGNORE INTO favourites(path, added) VALUES(?,?)", (f["path"], int(f.get("added", now))))
        for k, v in (data.get("settings") or {}).items():
            c.execute("INSERT OR REPLACE INTO settings(k, v) VALUES(?,?)", (str(k), str(v)))
        c.execute("COMMIT")
    finally:
        c.close()
    return True


def integrity_report(full=True):
    """Used by `heliograph doctor`: returns (ok, message)."""
    try:
        c = connect(readonly=True)
        try:
            res = c.execute("PRAGMA integrity_check" if full else "PRAGMA quick_check").fetchall()
        finally:
            c.close()
    except sqlite3.Error as e:
        return False, str(e)
    msg = "; ".join(r[0] for r in res[:5])
    return msg == "ok", msg
