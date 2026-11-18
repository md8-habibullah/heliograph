"""Small aggregate tables (per-folder counts, per-month counts) so the sidebar never has to scan
the whole `files` table - essential once a library has hundreds of thousands of rows."""

import sqlite3
import time

from .util import natkey, subtree_args, subtree_sql, within

_last = 0.0


def refresh(conn, force=True):
    """Recompute dirstats and monthstats in one transaction (readers keep seeing the old rows)."""
    global _last
    if not force and time.time() - _last < 45:
        return False
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM dirstats")
        conn.execute("INSERT INTO dirstats(dir, lib, n_image, n_video, n_audio) "
                     "SELECT dir, MAX(lib), SUM(kind='image'), SUM(kind='video'), SUM(kind='audio') "
                     "FROM files GROUP BY dir")
        conn.execute("DELETE FROM monthstats")
        conn.execute("INSERT INTO monthstats(ym, n) SELECT strftime('%Y-%m', ts,'unixepoch','localtime'), COUNT(*) "
                     "FROM files WHERE lib=1 GROUP BY 1")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    _last = time.time()
    try:                                                   # cheap when nothing changed; teaches the planner
        conn.execute("PRAGMA analysis_limit=400")         # how selective a date range or folder is
        conn.execute("PRAGMA optimize")
    except sqlite3.Error:
        pass
    return True


def totals(conn):
    t = conn.execute("SELECT COALESCE(SUM(n_image),0), COALESCE(SUM(n_video),0), COALESCE(SUM(n_audio),0) "
                     "FROM dirstats WHERE lib=1").fetchone()
    now = int(time.time())
    # driven from the (small) favourites table; a plain JOIN lets the planner scan every file instead
    fav = conn.execute("SELECT COUNT(*) FROM favourites f WHERE EXISTS "
                       "(SELECT 1 FROM files x WHERE x.path=f.path AND x.lib=1)").fetchone()[0]
    shots = conn.execute("SELECT COUNT(*) FROM files WHERE shot=1 AND lib=1").fetchone()[0]
    recent = conn.execute("SELECT COUNT(*) FROM files WHERE lib=1 AND ts>=?", (now - 30 * 86400,)).fetchone()[0]
    return {"all": t[0] + t[1] + t[2], "image": t[0], "video": t[1], "audio": t[2], "fav": fav,
            "shots": shots, "recent": recent}


def children(conn, parent, kind=None):
    """Immediate sub-folders of `parent` with item counts aggregated over their whole subtree.
    Returns (self_counts, [ {name, path, i, v, a} ... ] sorted naturally)."""
    parent = parent.rstrip("/")
    cut = len(parent) + 1
    subs = {}
    own = [0, 0, 0]
    for r in conn.execute("SELECT dir, n_image, n_video, n_audio FROM dirstats WHERE " + subtree_sql(),
                          subtree_args(parent)):
        c = (r[1], r[2], r[3])
        if r[0] == parent:
            own = list(c)
            continue
        rel = r[0][cut:]
        name = rel.split("/", 1)[0]
        e = subs.setdefault(name, [0, 0, 0])
        e[0] += c[0]
        e[1] += c[1]
        e[2] += c[2]
    out = []
    for name in sorted(subs, key=natkey):
        c = subs[name]
        if kind == "image":
            c = [c[0], 0, 0]
        elif kind == "video":
            c = [0, c[1], 0]
        elif kind == "audio":
            c = [0, 0, c[2]]
        if sum(c) == 0:
            continue
        out.append({"name": name, "path": parent + "/" + name, "i": c[0], "v": c[1], "a": c[2]})
    return own, out


def root_counts(conn, root_path):
    r = conn.execute("SELECT COALESCE(SUM(n_image),0)+COALESCE(SUM(n_video),0)+COALESCE(SUM(n_audio),0) "
                     "FROM dirstats WHERE " + subtree_sql(), subtree_args(root_path)).fetchone()
    return r[0]


__all__ = ["refresh", "totals", "children", "root_counts", "within"]
