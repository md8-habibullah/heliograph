"""Queries behind the UI: snapshot views, pages of items, folders, timeline, folder picker, roots.

A *view* is one scope (folder / kind / smart filter / date / search) sorted and grouped once.
Only the ids are kept, as a compact array of 8-byte integers (1 million files = 8 MB), plus the
list of groups. After that, any scroll position is a constant-time slice and a primary-key lookup
of ~120 rows - no OFFSET scanning - so a library of millions scrolls as smoothly as one of hundreds.
"""

import array
import json
import os
import shutil
import threading
import time
from collections import OrderedDict
from pathlib import Path

from . import db, stats
from .formats import KINDS, KIND_OF, ext_of, looks_like_media
from .state import RUNTIME, bump_version
from .util import ApiError, file_tag, local_ts, natkey, spawn, subtree_args, subtree_sql, to_int, within

SORT_COL = {"date": "ts", "name": "name_key", "size": "size", "type": "ext"}
G_EXPR = {"folder": "dir", "date": "strftime('%Y-%m', ts, 'unixepoch', 'localtime')", "type": "kind", "none": "''"}
KIND_RANK = "CASE kind WHEN 'image' THEN 0 WHEN 'video' THEN 1 ELSE 2 END"
MAX_VIEW_ROWS = 6_000_000
PAGE_MAX = 400


# --------------------------------------------------------------------------- where / order

def where_from(p):
    w, a = [], []
    d = (p.get("dir") or "").rstrip("/")
    if d:
        if p.get("sub", "1") != "0":
            w.append(subtree_sql())
            a += subtree_args(d)
        else:
            w.append("dir=?")
            a.append(d)
    else:
        w.append("lib=1")                              # global views never include "opened" folders
    if p.get("kind") in KINDS:
        w.append("kind=?")
        a.append(p["kind"])
    smart = p.get("smart")
    if smart == "fav":
        w.append("path IN (SELECT path FROM favourites)")
    elif smart == "shots":
        w.append("shot=1")
    elif smart == "recent":
        w.append("ts>=?")
        a.append(int(time.time()) - 30 * 86400)
    y, m = str(p.get("year", "")), str(p.get("month", ""))
    if y.isdigit() and 1970 <= int(y) <= 2100:
        yi = int(y)
        if m.isdigit() and 1 <= int(m) <= 12:
            mi = int(m)
            lo, hi = local_ts(yi, mi, 1), local_ts(yi + (mi == 12), mi % 12 + 1, 1)
        else:
            lo, hi = local_ts(yi, 1, 1), local_ts(yi + 1, 1, 1)
        w.append("ts>=? AND ts<?")
        a += [lo, hi]
    for term in str(p.get("q") or "").strip().lower().split()[:6]:
        w.append("instr(search, ?)>0")
        a.append(term)
    return (" WHERE " + " AND ".join(w)) if w else "", a


def order_from(group, sort, order):
    o = "ASC" if order == "asc" else "DESC"
    parts = []
    if group == "folder":
        parts += ["dir_key ASC", "dir ASC"]
    elif group == "date" and sort != "date":
        parts.append("strftime('%Y-%m', ts, 'unixepoch', 'localtime') DESC")
    elif group == "type":
        parts.append(KIND_RANK)
    col = SORT_COL.get(sort, "ts")
    parts.append(f"{col} {o}")
    if col != "name_key":
        parts.append("name_key ASC")
    parts.append("id ASC")
    return " ORDER BY " + ", ".join(parts)


def normalize(p):
    group = p.get("group") if p.get("group") in G_EXPR else "none"
    sort = p.get("sort") if p.get("sort") in SORT_COL else "date"
    order = "asc" if p.get("order") == "asc" else "desc"
    keep = {k: str(p.get(k) or "") for k in ("dir", "kind", "smart", "year", "month", "q", "sub")}
    keep.update(group=group, sort=sort, order=order)
    return keep


# --------------------------------------------------------------------------- snapshot views

class View:
    __slots__ = ("id", "ids", "groups", "total", "version", "built", "params", "ms")

    def __init__(self, vid, ids, groups, version, params, ms):
        self.id, self.ids, self.groups, self.total = vid, ids, groups, len(ids)
        self.version, self.built, self.params, self.ms = version, time.time(), params, ms


_views = OrderedDict()
_views_lock = threading.Lock()
_build_locks = {}
_counter = [0]


def build_view(p):
    p = normalize(p)
    key = json.dumps(p, sort_keys=True) + f"|v{RUNTIME['version']}"
    with _views_lock:
        v = _views.get(key)
        if v:
            _views.move_to_end(key)
            return v
        lock = _build_locks.setdefault(key, threading.Lock())
    with lock:
        with _views_lock:
            v = _views.get(key)
            if v:
                return v
        t0 = time.time()
        where, args = where_from(p)
        group = p["group"]
        sql = f"SELECT id, {G_EXPR[group]} FROM files{where}{order_from(group, p['sort'], p['order'])} LIMIT {MAX_VIEW_ROWS}"
        ids = array.array("q")
        groups = []
        cur_g, n = object(), 0
        for rid, g in db.conn().execute(sql, args):
            ids.append(rid)
            if g != cur_g:
                if n:
                    groups.append([cur_g, n])
                cur_g, n = g, 0
            n += 1
        if n:
            groups.append([cur_g, n])
        with _views_lock:
            _counter[0] += 1
            v = View(f"{os.getpid()}-{_counter[0]}", ids, groups, RUNTIME["version"], p,
                     int((time.time() - t0) * 1000))
            _views[key] = v
            while len(_views) > 6:
                _views.popitem(last=False)
            _build_locks.pop(key, None)
        return v


def clear_cache():
    """Forget every cached snapshot (tests use this when they switch to another database)."""
    with _views_lock:
        _views.clear()
    bump_version()


def get_view(vid):
    with _views_lock:
        for v in _views.values():
            if v.id == vid:
                return v
    raise ApiError(410, "This view expired - reload it")


def api_view(p):
    v = build_view(p)
    return {"view": v.id, "total": v.total, "version": v.version, "ms": v.ms,
            "groups": [{"key": g, "count": n} for g, n in v.groups] if v.params["group"] != "none" else []}


ITEM_SQL = ("SELECT id,path,name,kind,ext,size,mtime,ts,w,h,dur,title,artist,album,"
            "EXISTS(SELECT 1 FROM favourites f WHERE f.path=files.path) AS fav FROM files WHERE id IN ({})")


def item_json(r):
    d = {"id": r["id"], "k": file_tag(r["path"], r["mtime"], r["size"]), "name": r["name"], "kind": r["kind"],
         "ext": r["ext"], "size": r["size"], "mtime": r["mtime"], "ts": r["ts"], "fav": bool(r["fav"])}
    for k in ("w", "h", "dur", "title", "artist", "album"):
        if r[k] is not None:
            d[k] = r[k]
    return d


def api_items(p):
    v = get_view(p.get("view", ""))
    try:
        offset = max(0, int(p.get("offset", 0)))
        limit = min(PAGE_MAX, max(1, int(p.get("limit", 120))))
    except ValueError:
        raise ApiError(400, "bad paging")
    ids = list(v.ids[offset:offset + limit])
    out = []
    if ids:
        rows = {r["id"]: r for r in db.conn().execute(ITEM_SQL.format(",".join("?" * len(ids))), ids)}
        out = [item_json(rows[i]) if i in rows else {"id": i, "gone": True} for i in ids]
    return {"items": out, "offset": offset, "view": v.id}


def api_ids(p):
    """Ids of a range of a view (for 'select all' / shift-click ranges / batch conversion)."""
    v = get_view(p.get("view", ""))
    try:
        a = max(0, int(p.get("from", 0)))
        b = min(v.total, int(p.get("to", v.total)))
    except ValueError:
        raise ApiError(400, "bad range")
    if b - a > 20000:
        raise ApiError(400, "select at most 20,000 files at once")
    return {"ids": list(v.ids[a:b])}


def api_locate(p):
    """Position of one file inside a view (so a file opened from outside lands on the right picture)."""
    v = get_view(p.get("view", ""))
    fid = to_int(p.get("id"), "id", 0)
    try:
        return {"index": v.ids.index(fid)}
    except ValueError:
        return {"index": -1}


# --------------------------------------------------------------------------- single file

def api_item(fid):
    c = db.conn()
    r = c.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    if not r:
        raise ApiError(404, "No such file")
    d = {k: r[k] for k in ("id", "path", "dir", "name", "ext", "kind", "size", "mtime", "ts", "taken",
                           "w", "h", "dur", "title", "artist", "album", "codec")}
    d["k"] = file_tag(r["path"], r["mtime"], r["size"])
    d["fav"] = bool(c.execute("SELECT 1 FROM favourites WHERE path=?", (r["path"],)).fetchone())
    d["exists"] = os.path.exists(r["path"])
    return d


def row_for(fid):
    """The file row for serving, after checking it still belongs to a folder that was opened."""
    c = db.conn()
    r = c.execute("SELECT id,path,dir,name,ext,kind,size,mtime,dur,w,h,codec FROM files WHERE id=?", (fid,)).fetchone()
    if not r:
        raise ApiError(404, "No such file")
    roots = [x[0] for x in c.execute("SELECT path FROM roots")]
    if not any(within(r["path"], x) for x in roots):
        raise ApiError(404, "No such file")
    if not os.path.isfile(r["path"]):
        raise ApiError(404, "File is gone")
    return r


def set_fav(fid, value):
    c = db.conn()
    r = c.execute("SELECT path FROM files WHERE id=?", (fid,)).fetchone()
    if not r:
        return {"ok": False}
    if value:
        c.execute("INSERT OR IGNORE INTO favourites(path, added) VALUES(?,?)", (r["path"], int(time.time())))
    else:
        c.execute("DELETE FROM favourites WHERE path=?", (r["path"],))
    db.schedule_snapshot()
    with _views_lock:                                    # a cached Favourites view would otherwise be stale
        for k in [k for k, v in _views.items() if v.params.get("smart") == "fav"]:
            del _views[k]
    return {"ok": True}


def do_open(fid, action):
    r = row_for(fid)
    path = r["path"]
    if action == "default":
        ok = spawn(["xdg-open", path])
    elif action == "mpv":
        exe = shutil.which("mpv")
        if not exe:
            raise ApiError(400, "mpv is not installed")
        ok = spawn([exe, "--force-window=yes", "--", path])
    elif action == "folder":
        ok = spawn(["dolphin", "--select", path]) if shutil.which("dolphin") else spawn(["xdg-open", r["dir"]])
    else:
        raise ApiError(400, "unknown action")
    if not ok:
        raise ApiError(500, "Could not start the program")
    return {"ok": True}


# --------------------------------------------------------------------------- folders / timeline / state

def api_folders(p):
    """Sub-folders of a folder: counts over their whole subtree and up to 4 cover images each."""
    d = (p.get("dir") or "").rstrip("/")
    if not d:
        raise ApiError(400, "dir required")
    kind = p.get("kind") if p.get("kind") in KINDS else None
    try:
        offset = max(0, int(p.get("offset", 0)))
        limit = min(300, max(1, int(p.get("limit", 60))))
    except ValueError:
        raise ApiError(400, "bad paging")
    c = db.conn()
    own, subs = stats.children(c, d, kind)
    page = subs[offset:offset + limit]
    if p.get("covers", "1") != "0":
        kind_sql, kind_args = (" AND kind=?", [kind]) if kind else ("", [])
        for f in page:
            rows = c.execute("SELECT id, path, mtime, size FROM files WHERE " + subtree_sql() + " AND kind IN ('image','video')" +
                             kind_sql + " LIMIT 4", subtree_args(f["path"]) + kind_args).fetchall()
            f["covers"] = [{"id": x["id"], "k": file_tag(x["path"], x["mtime"], x["size"])} for x in rows]
    return {"folders": page, "total": len(subs), "offset": offset,
            "own": {"i": own[0], "v": own[1], "a": own[2]}}


def api_years():
    rows = db.conn().execute("SELECT ym, n FROM monthstats ORDER BY ym DESC").fetchall()
    years = {}
    for r in rows:
        y, m = r["ym"].split("-")
        e = years.setdefault(y, {"year": y, "count": 0, "months": []})
        e["count"] += r["n"]
        e["months"].append({"month": m, "count": r["n"]})
    return {"years": list(years.values())}


def api_roots():
    c = db.conn()
    out = []
    for r in c.execute("SELECT id, path, persist, depth, last_scan FROM roots ORDER BY persist DESC, path"):
        out.append({"id": r["id"], "path": r["path"], "persist": bool(r["persist"]), "depth": r["depth"],
                    "online": os.path.isdir(r["path"]), "last_scan": r["last_scan"],
                    "count": stats.root_counts(c, r["path"])})
    return out


# --------------------------------------------------------------------------- roots (library / opened)

FORBIDDEN = ("/proc", "/sys", "/dev", "/run/user", "/boot", "/etc", "/usr", "/bin", "/sbin", "/lib", "/var/lib")


def validate_dir(path):
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        raise ApiError(400, "That is not a folder")
    if path == "/" or any(within(path, x) for x in FORBIDDEN):
        raise ApiError(400, "Pick a folder in your home or on a drive, not a system folder")
    if not os.access(path, os.R_OK | os.X_OK):
        raise ApiError(403, "That folder cannot be read")
    return path


def _root_covering(c, path):
    """A root that already indexes `path` (whole tree under it), or None."""
    best = None
    for r in c.execute("SELECT id, path, persist, depth FROM roots"):
        if within(path, r["path"]) and (r["depth"] < 0 or path == r["path"]):
            if best is None or len(r["path"]) > len(best["path"]):
                best = r
    return best


def open_folder(indexer, path, persist):
    """Open a folder. persist=True adds it to the library; False opens it for this session only."""
    path = validate_dir(path)
    c = db.conn()
    cover = _root_covering(c, path)
    now = int(time.time())
    if cover is not None and cover["depth"] < 0 and (cover["persist"] or not persist):
        indexer.request_scan(cover["id"], min_age=60)          # already indexed as part of a bigger root
        return {"added": False, "root_id": cover["id"], "dir": path}
    c.execute("BEGIN IMMEDIATE")
    try:
        row = c.execute("SELECT id, persist, depth FROM roots WHERE path=?", (path,)).fetchone()
        if row:
            rid = row["id"]
            if persist and not row["persist"]:
                c.execute("UPDATE roots SET persist=1 WHERE id=?", (rid,))
                c.execute("UPDATE files SET lib=1 WHERE root_id=?", (rid,))
            if row["depth"] != -1:
                c.execute("UPDATE roots SET depth=-1 WHERE id=?", (rid,))
        else:
            for r in c.execute("SELECT id, path, persist FROM roots").fetchall():   # a new root absorbs roots inside it
                if within(r["path"], path) and (persist or not r["persist"]):
                    c.execute("DELETE FROM roots WHERE id=?", (r["id"],))
            rid = c.execute("INSERT INTO roots(path, persist, depth, added) VALUES(?,?,-1,?)",
                            (path, 1 if persist else 0, now)).lastrowid
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    if persist:
        db.schedule_snapshot()
    bump_version()
    indexer.request_scan(rid)
    return {"added": True, "root_id": rid, "dir": path}


def open_file(indexer, path):
    """Open one media file (e.g. from a file manager): index it at once so it can be shown, and its
    folder (one level only) in the background so the arrow keys can step through its neighbours."""
    from .indexer import index_one
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        raise ApiError(400, "That is not a file")
    ext = ext_of(os.path.basename(path))
    if not KIND_OF.get(ext) or not looks_like_media(ext, path):
        raise ApiError(400, "That does not look like a picture, video or audio file")
    d = os.path.dirname(path)
    validate_dir(d)
    c = db.conn()
    cover = _root_covering(c, d)
    if cover is None:
        c.execute("INSERT OR IGNORE INTO roots(path, persist, depth, added) VALUES(?,0,0,?)", (d, int(time.time())))
        row = c.execute("SELECT id, persist FROM roots WHERE path=?", (d,)).fetchone()
        rid, lib = row["id"], row["persist"]
        bump_version()
    else:
        rid, lib = cover["id"], cover["persist"]
    fid = index_one(rid, lib, path)
    if cover is None:
        indexer.request_scan(rid)
    return {"dir": d, "id": fid, "root_id": rid}


def remove_root(rid):
    c = db.conn()
    r = c.execute("SELECT path FROM roots WHERE id=?", (rid,)).fetchone()
    if not r:
        raise ApiError(404, "No such folder in the library")
    c.execute("BEGIN IMMEDIATE")
    c.execute("DELETE FROM roots WHERE id=?", (rid,))          # files go with it (ON DELETE CASCADE)
    c.execute("COMMIT")
    stats.refresh(c)
    db.schedule_snapshot()
    bump_version()
    return {"ok": True}


def purge_session_roots():
    """Folders that were only opened for a session are forgotten when the service starts."""
    c = db.conn()
    c.execute("DELETE FROM roots WHERE persist=0")


# --------------------------------------------------------------------------- folder / file picker

def api_fs(p):
    """List a directory for the picker: sub-folders, plus media files in 'file' mode."""
    path = os.path.abspath(os.path.expanduser(p.get("path") or str(Path.home())))
    if not os.path.isdir(path):
        raise ApiError(400, "Not a folder")
    want_files = p.get("mode") == "file"
    dirs, files = [], []
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.name.startswith("."):
                        continue
                    if e.is_dir():
                        dirs.append({"name": e.name, "path": e.path})
                    elif want_files and KIND_OF.get(ext_of(e.name)) and e.is_file():
                        files.append({"name": e.name, "path": e.path, "kind": KIND_OF[ext_of(e.name)]})
                except OSError:
                    pass
    except OSError as ex:
        raise ApiError(403, ex.strerror or "Cannot read folder")
    home = Path.home()
    quick = []
    for n in ("Pictures", "Videos", "Music", "Downloads", "Documents", "Desktop"):
        if (home / n).is_dir():
            quick.append({"name": n, "path": str(home / n)})
    user = os.environ.get("USER") or home.name
    for base in (f"/run/media/{user}", f"/media/{user}", "/mnt"):
        try:
            for e in sorted(os.scandir(base), key=lambda x: x.name):
                if e.is_dir():
                    quick.append({"name": e.name + " (drive)", "path": e.path})
        except OSError:
            pass
    return {"path": path, "parent": os.path.dirname(path) if path != "/" else None,
            "dirs": sorted(dirs, key=lambda x: natkey(x["name"]))[:3000],
            "files": sorted(files, key=lambda x: natkey(x["name"]))[:3000],
            "quick": quick, "home": str(home)}
