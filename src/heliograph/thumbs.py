"""Thumbnails and previews.

Thumbnails (400 px JPEG) live as blobs in their own SQLite file, `thumbs.db` in the cache folder:
one file instead of hundreds of thousands, cheap to prune (least recently used first) and to delete,
and a failed attempt is remembered so a broken file is not retried on every scroll.
Larger "previews" (2400 px, for pictures a browser cannot show) are ordinary files, LRU pruned.

Nothing here runs unless something asks for it: a thumbnail is made when the window scrolls to it.
How many are made at once is decided by the governor (1 to 3), every helper runs at low priority,
and a request whose browser connection has already gone away is dropped before any work starts.
"""

import hashlib
import io
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from . import db
from .formats import PIL_EXT, RAW_EXT
from .paths import paths
from .state import GOVERNOR, LOCK, STATUS
from .util import low_prefix, run_low

THUMB_PX = 400
PREVIEW_PX = 2400
NEGATIVE_TTL = 7 * 86400
BG = (30, 33, 38)

_tls = threading.local()
_touch = {}
_touch_lock = threading.Lock()
_last_flush = time.time()
_inserts = 0


# --------------------------------------------------------------------------- storage

def close_thread_connection():
    """Close this thread's thumbnail-store connection (request threads call it when their HTTP connection ends)."""
    c = getattr(_tls, "conn", None)
    _tls.conn = None
    if c is not None:
        try:
            c.close()
        except sqlite3.Error:
            pass


def _conn():
    c = getattr(_tls, "conn", None)
    p = str(paths().thumbs_db)
    if c is None or getattr(_tls, "path", None) != p:
        close_thread_connection()
        paths().ensure()
        fresh = not Path(p).exists()
        c = sqlite3.connect(p, timeout=30, isolation_level=None, check_same_thread=False)
        if fresh:
            c.execute("PRAGMA auto_vacuum=INCREMENTAL")        # only possible before the first table
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA cache_size=-4000")
        c.execute("PRAGMA mmap_size=0")
        c.execute("CREATE TABLE IF NOT EXISTS thumb(k INTEGER PRIMARY KEY, chk INTEGER NOT NULL, "
                  "data BLOB NOT NULL, ok INTEGER NOT NULL, used INTEGER NOT NULL, created INTEGER NOT NULL)")
        c.execute("CREATE INDEX IF NOT EXISTS thumb_used ON thumb(used)")
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        _tls.conn, _tls.path = c, p
    return c


def key_for(row, px):
    d = hashlib.sha1(f"{row['path']}\0{row['mtime']}\0{row['size']}\0{px}".encode()).digest()
    return int.from_bytes(d[:8], "big", signed=True), int.from_bytes(d[8:12], "big")


def lookup(row, px):
    """(state, data): ('hit', bytes) | ('failed', None) | ('miss', None)."""
    k, chk = key_for(row, px)
    r = _conn().execute("SELECT chk, data, ok, created FROM thumb WHERE k=?", (k,)).fetchone()
    if not r or r[0] != chk:
        return "miss", None
    now = int(time.time())
    if r[2]:
        with _touch_lock:
            _touch[k] = now
        return "hit", bytes(r[1])
    if now - r[3] > NEGATIVE_TTL:
        return "miss", None
    return "failed", None


def store(row, px, data):
    global _inserts
    k, chk = key_for(row, px)
    now = int(time.time())
    _conn().execute("INSERT OR REPLACE INTO thumb(k,chk,data,ok,used,created) VALUES(?,?,?,?,?,?)",
                    (k, chk, data or b"", 1 if data else 0, now, now))
    _inserts += 1
    if _inserts % 400 == 0:
        threading.Thread(target=prune, daemon=True).start()


def flush_touch():
    """Write 'last used' times in one batch (so reading a thumbnail does not write to disk each time)."""
    global _last_flush
    with _touch_lock:
        items = list(_touch.items())
        _touch.clear()
    _last_flush = time.time()
    if not items:
        return
    c = _conn()
    c.execute("BEGIN")
    c.executemany("UPDATE thumb SET used=? WHERE k=?", [(t, k) for k, t in items])
    c.execute("COMMIT")


def maybe_flush():
    if time.time() - _last_flush > 30:
        flush_touch()


def disk_bytes():
    p = paths().thumbs_db
    total = 0
    for suffix in ("", "-wal"):
        try:
            total += p.with_name(p.name + suffix).stat().st_size
        except OSError:
            pass
    return total


def prune(max_mb=None):
    """Delete the least recently used thumbnails until the file is under 80% of the limit."""
    try:
        limit = int(float(max_mb if max_mb is not None else db.get_setting("thumb_cache_mb", "2048")) * 1024 * 1024)
        flush_touch()
        size = disk_bytes()
        if size <= limit:
            return 0
        c = _conn()
        target = int(limit * 0.8)
        avg = max(1, c.execute("SELECT COALESCE(AVG(length(data)),1) FROM thumb").fetchone()[0])
        n = int((size - target) / avg) + 1
        c.execute("DELETE FROM thumb WHERE k IN (SELECT k FROM thumb ORDER BY used LIMIT ?)", (n,))
        c.execute("PRAGMA incremental_vacuum(20000)")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return n
    except sqlite3.Error:
        return 0


def count():
    try:
        return _conn().execute("SELECT COUNT(*) FROM thumb WHERE ok=1").fetchone()[0]
    except sqlite3.Error:
        return 0


def clear():
    c = _conn()
    n = c.execute("SELECT COUNT(*) FROM thumb").fetchone()[0]
    c.execute("DELETE FROM thumb")
    c.execute("PRAGMA incremental_vacuum")
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    pv = paths().previews
    if pv.exists():
        shutil.rmtree(pv, ignore_errors=True)
        pv.mkdir(parents=True, exist_ok=True, mode=0o700)
    return n


# --------------------------------------------------------------------------- generators

def _flatten(im):
    from PIL import Image
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        base = Image.new("RGBA", im.size, BG + (255,))
        return Image.alpha_composite(base, im).convert("RGB")
    if im.mode != "RGB":
        if im.mode == "I" or im.mode.startswith("I;16"):
            im = im.point(lambda p: p / 256).convert("L")
        im = im.convert("RGB")
    return im


def _lanczos(image_module):
    """The LANCZOS filter on any Pillow: Image.Resampling.LANCZOS (9.1 and newer) or Image.LANCZOS (older, such as
    the 9.0.1 that Ubuntu 22.04 ships)."""
    return getattr(image_module, "Resampling", image_module).LANCZOS


def _jpeg_bytes(im, px, quality):
    from PIL import Image
    im = _flatten(im)
    im.thumbnail((px, px), _lanczos(Image), reducing_gap=2.0)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=quality, optimize=False)
    return out.getvalue()


def _pil_make(src, px, quality):
    from PIL import Image, ImageOps
    with Image.open(src) as im:
        try:
            im.draft("RGB", (px, px))                # JPEG: decode at reduced size, much faster
        except Exception:
            pass
        im = ImageOps.exif_transpose(im)
        return _jpeg_bytes(im, px, quality)


def _magick_make(src, px, quality):
    exe = shutil.which("magick")
    if not exe:
        return None
    rc, out = run_low([exe, "-limit", "memory", "256MiB", "-limit", "map", "512MiB", "-limit", "disk", "1GiB",
                       "-limit", "thread", "2", f"{src}[0]", "-auto-orient", "-thumbnail", f"{px}x{px}>",
                       "-background", "#1e2126", "-alpha", "remove", "-alpha", "off", "-quality", str(quality),
                       "jpg:-"], 60)
    return out if rc == 0 and out else None


def _dcraw_make(src, px, quality):
    """RAW through LibRaw's dcraw_emu at half size (fast), then Pillow for the final scale."""
    exe = shutil.which("dcraw_emu")
    if not exe:
        return None
    rc, out = run_low([exe, "-c", "-h", "-w", src], 90)
    if rc != 0 or not out:
        return None
    try:
        from PIL import Image
        with Image.open(io.BytesIO(out)) as im:
            return _jpeg_bytes(im, px, quality)
    except Exception:
        return None


def _heif_make(src, px, quality):
    exe = shutil.which("heif-convert")
    if not exe:
        return None
    with tempfile.TemporaryDirectory(prefix="heliograph-") as td:
        dst = os.path.join(td, "o.jpg")
        rc, _ = run_low([exe, "-q", "85", src, dst], 60)
        if rc != 0:
            return None
        found = [f for f in os.listdir(td) if f.endswith(".jpg")]
        if not found:
            return None
        try:
            return _pil_make(os.path.join(td, sorted(found)[0]), px, quality)
        except Exception:
            return None


def _scale_filter(px):
    return f"scale='if(gt(iw,ih),min({px},iw),-2)':'if(gt(iw,ih),-2,min({px},ih))'"


def _ffmpeg_make(src, px, seek=0.0, quality=4, keyframe=False):
    exe = shutil.which("ffmpeg")
    if not exe:
        return None
    cmd = [exe, "-nostdin", "-v", "error", "-threads", "2"]
    if seek > 0:
        cmd += ["-ss", f"{seek:.2f}"]
        if keyframe:
            cmd += ["-noaccurate_seek"]
    if keyframe:
        cmd += ["-skip_frame", "nokey"]
    cmd += ["-i", src, "-an", "-sn", "-frames:v", "1", "-vf", _scale_filter(px), "-q:v", str(quality),
            "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"]
    rc, out = run_low(cmd, 45)
    return out if rc == 0 and out else None


def generate(row, px):
    """JPEG bytes for the file, or None when it cannot be decoded."""
    kind, ext, src = row["kind"], row["ext"], row["path"]
    quality = 82 if px <= THUMB_PX else 88
    data = None
    if kind == "image":
        if ext in PIL_EXT:
            try:
                data = _pil_make(src, px, quality)
            except Exception:
                data = None
        if not data and ext in RAW_EXT:
            data = _magick_make(src, px, quality) or _dcraw_make(src, px, quality)
        if not data and ext in ("heic", "heif", "heics", "heifs", "hif", "avif", "avci"):
            data = _ffmpeg_make(src, px) or _heif_make(src, px, quality) or _magick_make(src, px, quality)
        if not data:
            data = _magick_make(src, px, quality) or _ffmpeg_make(src, px)
    elif kind == "video":
        dur = row["dur"]
        if dur is None:
            from .indexer import probe_now
            dur = (probe_now(row) or {}).get("dur") or 0
        seek = min(dur * 0.12, 90.0) if dur and dur > 4 else 0.0
        data = _ffmpeg_make(src, px, seek, keyframe=seek > 0)
        if not data:
            data = _ffmpeg_make(src, px, seek)
        if not data and seek:
            data = _ffmpeg_make(src, px, 0.0)
    else:                                            # audio: embedded cover art if there is one
        data = _ffmpeg_make(src, px)
    return data


# --------------------------------------------------------------------------- scheduling

class _Limiter:
    """Counting semaphore whose limit follows the governor (so a busy PC gets fewer parallel jobs)."""

    def __init__(self):
        self.cv = threading.Condition()
        self.n = 0

    def acquire(self, alive):
        with self.cv:
            STATUS["thumbs"]["queued"] += 1
            try:
                while self.n >= GOVERNOR.foreground_workers():
                    self.cv.wait(0.5)
                    if not alive():
                        return False
                self.n += 1
                return True
            finally:
                STATUS["thumbs"]["queued"] -= 1

    def release(self):
        with self.cv:
            self.n -= 1
            self.cv.notify()


_LIMITER = _Limiter()
_key_locks = {}
_key_guard = threading.Lock()


def get_thumb(row, px=THUMB_PX, alive=lambda: True):
    """Return ('ok', jpeg_bytes) | ('failed', None) | ('gone', None). Makes the thumbnail if needed."""
    maybe_flush()
    state, data = lookup(row, px)
    if state == "hit":
        with LOCK:
            STATUS["thumbs"]["cached"] += 1
        return "ok", data
    if state == "failed":
        return "failed", None
    k, _ = key_for(row, px)
    with _key_guard:
        lock = _key_locks.setdefault(k, threading.Lock())
    try:
        with lock:
            state, data = lookup(row, px)                # another request may have made it meanwhile
            if state == "hit":
                return "ok", data
            if state == "failed":
                return "failed", None
            if not alive():
                return "gone", None
            if not _LIMITER.acquire(alive):
                return "gone", None
            t0 = time.time()
            with LOCK:
                STATUS["thumbs"]["inflight"] += 1
            try:
                data = generate(row, px)
            finally:
                with LOCK:
                    STATUS["thumbs"]["inflight"] -= 1
                _LIMITER.release()
            with LOCK:
                STATUS["thumbs"]["made" if data else "failed"] += 1
                STATUS["thumbs"]["ms_total"] += int((time.time() - t0) * 1000)
            store(row, px, data)
            return ("ok", data) if data else ("failed", None)
    finally:
        with _key_guard:
            _key_locks.pop(k, None)


# --------------------------------------------------------------------------- previews (files)

PREVIEW_LIMIT = 600 * 1024 * 1024


def preview_path(row, alive=lambda: True):
    """Path of a 2400 px JPEG of the file (made on first request), or None."""
    d = hashlib.sha1(f"{row['path']}\0{row['mtime']}\0{row['size']}\0{PREVIEW_PX}".encode()).hexdigest()
    dst = paths().previews / d[:2] / (d + ".jpg")
    if dst.exists():
        try:
            os.utime(dst)
        except OSError:
            pass
        return dst
    with _key_guard:
        lock = _key_locks.setdefault(("p", d), threading.Lock())
    try:
        with lock:
            if dst.exists():
                return dst
            if not alive() or not _LIMITER.acquire(alive):
                return None
            try:
                data = generate(row, PREVIEW_PX)
            finally:
                _LIMITER.release()
            if not data:
                return None
            dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = dst.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, dst)
            threading.Thread(target=prune_previews, daemon=True).start()
            return dst
    finally:
        with _key_guard:
            _key_locks.pop(("p", d), None)


def prune_previews(limit=PREVIEW_LIMIT):
    root = paths().previews
    files = []
    total = 0
    try:
        for sub in root.iterdir():
            if sub.is_dir():
                for f in sub.iterdir():
                    try:
                        s = f.stat()
                    except OSError:
                        continue
                    files.append((s.st_mtime, s.st_size, f))
                    total += s.st_size
    except OSError:
        return 0
    if total <= limit:
        return 0
    files.sort()
    removed = 0
    for _, size, f in files:
        if total <= limit * 0.8:
            break
        try:
            f.unlink()
            total -= size
            removed += 1
        except OSError:
            pass
    return removed


def cache_summary():
    pv = 0
    try:
        for sub in paths().previews.iterdir():
            if sub.is_dir():
                pv += sum(f.stat().st_size for f in sub.iterdir() if f.is_file())
    except OSError:
        pass
    return {"thumbs": count(), "thumbs_bytes": disk_bytes(), "previews_bytes": pv}


__all__ = ["get_thumb", "preview_path", "prune", "clear", "cache_summary", "close_thread_connection", "THUMB_PX", "PREVIEW_PX", "low_prefix"]
