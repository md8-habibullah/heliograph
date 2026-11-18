"""Finds media files (scan) and reads their details (enrich), without ever hogging the machine.

Scan     walks a root with os.scandir and stores name / size / date only. That is cheap and gives a
         usable library immediately; it runs at low priority and takes short naps when the PC is busy.
Enrich   fills in pixel size, duration, taken date and tags. Order of work:
           1. files that are on screen right now (the UI reports them via want())  - always, quickly;
           2. everything cheap to read (picture headers, MP4/MOV headers)           - when not busy,
              as short bursts with a duty cycle (sleep = work time x ratio);
           3. everything that needs ffprobe (MKV, AVI, MP3, ...)                    - only while the
              machine is idle, never on battery, never while the user pauses indexing.
         The work is resumable: unread files simply stay at meta=0 and are picked up next time.
"""

import os
import queue
import stat as statmod
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from . import db, stats
from .formats import AMBIGUOUS_EXT, KIND_OF, SKIP_DIRS, looks_like_media
from .meta import cost_of, probe_cheap, probe_ffprobe
from .state import GOVERNOR, STATUS, bump_stats, bump_version
from .util import is_screenshot, lower_this_thread, natkey, subtree_args, subtree_sql, within

UPSERT = """
INSERT INTO files(root_id,lib,path,dir,name,ext,kind,size,mtime,ts,name_key,dir_key,search,shot,cost,seen,added)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(path) DO UPDATE SET
  seen=excluded.seen,
  root_id = CASE WHEN excluded.lib >= files.lib THEN excluded.root_id ELSE files.root_id END,
  lib = MAX(files.lib, excluded.lib),
  ts   = CASE WHEN files.size=excluded.size AND files.mtime=excluded.mtime THEN files.ts ELSE excluded.ts END,
  taken= CASE WHEN files.size=excluded.size AND files.mtime=excluded.mtime THEN files.taken ELSE NULL END,
  meta = CASE WHEN files.size=excluded.size AND files.mtime=excluded.mtime THEN files.meta ELSE 0 END,
  size=excluded.size, mtime=excluded.mtime
"""

UPDATE_META = ("UPDATE files SET w=?,h=?,dur=?,taken=?,ts=COALESCE(?,mtime),title=?,artist=?,album=?,codec=?,"
               "search=?,meta=? WHERE id=? AND mtime=?")

FLUSH_EVERY = 2000

_gen_lock = threading.Lock()
_last_gen = [0]


def next_gen():
    """A scan generation: milliseconds since the epoch, but strictly increasing even for two scans in one millisecond
    (files not seen in the newest scan are the ones that vanished, so two scans must never share a number)."""
    with _gen_lock:
        _last_gen[0] = max(int(time.time() * 1000), _last_gen[0] + 1)
        return _last_gen[0]


class Indexer:
    def __init__(self):
        self.scan_q = queue.Queue()
        self.pending = set()
        self.lock = threading.Lock()
        self.want = deque(maxlen=600)
        self.wake = threading.Event()
        self.stop_flag = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="probe")
        self.threads = [threading.Thread(target=self._scan_loop, daemon=True, name="scan"),
                        threading.Thread(target=self._enrich_loop, daemon=True, name="enrich")]

    def start(self):
        for t in self.threads:
            t.start()

    def stop(self):
        self.stop_flag.set()
        self.wake.set()
        self.pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ requests
    def request_scan(self, root_id=None, min_age=0):
        """Queue a scan of one root (or all persistent roots when root_id is None)."""
        c = db.conn()
        if root_id is None:
            rows = c.execute("SELECT id, last_scan FROM roots").fetchall()
        else:
            rows = c.execute("SELECT id, last_scan FROM roots WHERE id=?", (root_id,)).fetchall()
        for r in rows:
            if min_age and time.time() - r["last_scan"] < min_age:
                continue
            with self.lock:
                if r["id"] in self.pending:
                    continue
                self.pending.add(r["id"])
            self.scan_q.put(r["id"])

    def want_ids(self, ids):
        """The UI says these files are on screen: read their details first."""
        for i in ids[:200]:
            self.want.append(int(i))
        self.wake.set()

    # ------------------------------------------------------------------ scan thread
    def _scan_loop(self):
        lower_this_thread(10)
        while not self.stop_flag.is_set():
            try:
                rid = self.scan_q.get(timeout=1.0)
            except queue.Empty:
                continue
            with self.lock:
                self.pending.discard(rid)
            try:
                self.scan(rid)
            except Exception:
                traceback.print_exc()
                STATUS["scan"].update(state="idle")
                time.sleep(1)

    def scan(self, rid):
        conn = db.conn()
        row = conn.execute("SELECT path, persist, depth FROM roots WHERE id=?", (rid,)).fetchone()
        if not row:
            return
        root, lib, max_depth = row["path"], row["persist"], row["depth"]
        st = STATUS["scan"]
        if not os.path.isdir(root) or not os.access(root, os.R_OK | os.X_OK):
            return                                   # drive unplugged etc.: keep what we know
        st.update(state="scanning", root=root, files=0, dirs=0, started=time.time())
        gen = next_gen()
        now = int(time.time())
        root_real = os.path.realpath(root)
        unreadable = []
        batch = []
        stack = [(root, 0)]

        def flush():
            if not batch:
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.executemany(UPSERT, batch)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            batch.clear()
            bump_version()
            if GOVERNOR.snapshot()["state"] == "busy":
                time.sleep(0.15)                      # let the desktop breathe between batches

        while stack and not self.stop_flag.is_set():
            d, depth = stack.pop()
            try:
                it = os.scandir(d)
            except FileNotFoundError:
                continue
            except OSError:
                unreadable.append(d)
                continue
            st["dirs"] += 1
            with it:
                for e in it:
                    name = e.name
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if name.startswith(".") or name in SKIP_DIRS:
                                continue
                            if max_depth < 0 or depth < max_depth:
                                stack.append((e.path, depth + 1))
                            continue
                        if name.startswith("."):
                            continue
                        dot = name.rfind(".")
                        ext = name[dot + 1:].lower() if dot > 0 else ""
                        kind = KIND_OF.get(ext)
                        if not kind:
                            continue
                        if ext in AMBIGUOUS_EXT and not looks_like_media(ext, e.path):
                            continue                  # e.g. TypeScript source named *.ts
                        s = e.stat()
                        if not statmod.S_ISREG(s.st_mode):
                            continue
                        if e.is_symlink() and not within(os.path.realpath(e.path), root_real):
                            continue                  # never index files outside the chosen folder
                    except OSError:
                        continue
                    mt = int(s.st_mtime)
                    batch.append((rid, lib, e.path, d, name, ext, kind, s.st_size, mt, mt, natkey(name), natkey(d),
                                  (name + "\n" + d).lower(), 1 if is_screenshot(name, d) else 0,
                                  cost_of(kind, ext), gen, now))
                    st["files"] += 1
                    if len(batch) >= FLUSH_EVERY:
                        flush()
        flush()
        if self.stop_flag.is_set():
            st.update(state="idle")
            return
        # Remove files that vanished, but never from a folder we could not read.
        sql = "DELETE FROM files WHERE root_id=? AND seen<?"
        args = [rid, gen]
        for u in unreadable:
            sql += " AND NOT " + subtree_sql()
            args += subtree_args(u)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(sql, args)
        conn.execute("UPDATE roots SET last_scan=? WHERE id=?", (int(time.time()), rid))
        conn.execute("COMMIT")
        stats.refresh(conn)
        st.update(state="idle", finished=time.time(), last_files=st["files"],
                  last_seconds=round(time.time() - st["started"], 2))
        bump_version()
        self.wake.set()

    # ------------------------------------------------------------------ enrich thread
    def _enrich_loop(self):
        lower_this_thread(12)
        st = STATUS["enrich"]
        last_count = 0.0
        done_since_stats = 0
        while not self.stop_flag.is_set():
            try:
                if self._do_wanted():
                    continue
                snap = GOVERNOR.snapshot()
                if time.time() - last_count > 3:
                    last_count = time.time()
                    st["remaining"] = db.conn().execute("SELECT COUNT(*) FROM files WHERE meta=0").fetchone()[0]
                if st["remaining"] == 0:
                    if done_since_stats:
                        stats.refresh(db.conn())
                        bump_stats()
                        done_since_stats = 0
                    st.update(state="idle", reason="", current=None)
                    self.wake.wait(4.0)
                    self.wake.clear()
                    last_count = 0
                    continue
                if snap["state"] == "paused":
                    st.update(state="paused", reason="paused by you")
                    self.wake.wait(3.0)
                    self.wake.clear()
                    continue
                if snap["state"] == "busy":
                    st.update(state="waiting", reason=", ".join(snap["reasons"]))
                    self.wake.wait(4.0)
                    self.wake.clear()
                    continue
                t0 = time.monotonic()
                n = self._do_background(cheap_only=not GOVERNOR.expensive_ok(), limit=60 if snap["state"] == "idle" else 20)
                if n == 0:
                    # only expensive files are left and the machine is not idle: wait for a quiet moment
                    st.update(state="waiting", reason="waiting for an idle moment to read MKV/AVI/audio details")
                    self.wake.wait(5.0)
                    self.wake.clear()
                    continue
                st.update(state="running", reason="")
                st["remaining"] = max(0, st["remaining"] - n)
                st["done"] += n
                done_since_stats += n
                if done_since_stats >= 5000 and time.time() - stats._last > 60:
                    stats.refresh(db.conn())
                    bump_stats()
                    done_since_stats = 0
                time.sleep(min(2.0, (time.monotonic() - t0) * GOVERNOR.pause_ratio()))
            except Exception:
                traceback.print_exc()
                time.sleep(2)

    def _apply(self, results):
        """results: list of (row, details dict). Writes them in one transaction."""
        out = []
        for r, m in results:
            title, artist, album = m.get("title"), m.get("artist"), m.get("album")
            search = " ".join(x for x in (r["name"], r["dir"], title, artist, album) if x).lower()
            out.append((m.get("w"), m.get("h"), m.get("dur"), m.get("taken"), m.get("taken"), title, artist, album,
                        m.get("codec"), search, 1 if m else 2, r["id"], r["mtime"]))
        c = db.conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            c.executemany(UPDATE_META, out)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    def _probe(self, r):
        try:
            return r, probe_cheap(r["kind"], r["ext"], r["path"])
        except Exception:
            return r, {}

    def _do_wanted(self):
        """Read the details of files the UI is showing (even if the machine is busy: it is a few files)."""
        ids = []
        while self.want and len(ids) < 60:
            i = self.want.popleft()
            if i not in ids:
                ids.append(i)
        if not ids:
            return False
        ph = ",".join("?" * len(ids))
        rows = db.conn().execute(f"SELECT id,path,dir,name,kind,ext,mtime FROM files WHERE meta=0 AND id IN ({ph})",
                                 ids).fetchall()
        if not rows:
            return True
        self._apply(list(self.pool.map(self._probe, rows)))
        return True

    def _do_background(self, cheap_only, limit):
        c = db.conn()
        sql = ("SELECT id,path,dir,name,kind,ext,mtime FROM files WHERE meta=0 AND cost=1 ORDER BY id LIMIT ?")
        rows = c.execute(sql, (limit,)).fetchall()
        if not rows and not cheap_only:
            rows = c.execute("SELECT id,path,dir,name,kind,ext,mtime FROM files WHERE meta=0 AND cost=2 "
                             "ORDER BY id LIMIT ?", (max(4, limit // 6),)).fetchall()
        if not rows:
            return 0
        STATUS["enrich"]["current"] = os.path.dirname(rows[0]["path"])
        self._apply([self._probe(r) for r in rows])
        return len(rows)


def probe_now(row):
    """Read one file's details immediately and store them (used by thumbnails that need a duration)."""
    r = db.conn().execute("SELECT id,path,dir,name,kind,ext,mtime FROM files WHERE id=?", (row["id"],)).fetchone()
    if not r:
        return {}
    m = probe_cheap(r["kind"], r["ext"], r["path"]) or (probe_ffprobe(r["path"]) if r["kind"] != "image" else {})
    c = db.conn()
    title, artist, album = m.get("title"), m.get("artist"), m.get("album")
    search = " ".join(x for x in (r["name"], r["dir"], title, artist, album) if x).lower()
    c.execute("BEGIN IMMEDIATE")
    c.execute(UPDATE_META, (m.get("w"), m.get("h"), m.get("dur"), m.get("taken"), m.get("taken"), title, artist,
                            album, m.get("codec"), search, 1 if m else 2, r["id"], r["mtime"]))
    c.execute("COMMIT")
    return m


def index_one(root_id, lib, path):
    """Index a single file right now (opening a file from a file manager). Returns its id."""
    from .formats import ext_of
    name = os.path.basename(path)
    ext = ext_of(name)
    kind = KIND_OF.get(ext)
    if not kind or not looks_like_media(ext, path):
        raise ValueError("not a media file")
    s = os.stat(path)
    d = os.path.dirname(path)
    mt = int(s.st_mtime)
    gen = next_gen()
    c = db.conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        c.execute(UPSERT, (root_id, lib, path, d, name, ext, kind, s.st_size, mt, mt, natkey(name), natkey(d),
                           (name + "\n" + d).lower(), 1 if is_screenshot(name, d) else 0, cost_of(kind, ext),
                           gen, int(time.time())))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    bump_version()
    return c.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()[0]
