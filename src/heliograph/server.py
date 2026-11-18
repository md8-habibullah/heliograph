"""The local HTTP server: JSON API for the window plus media, thumbnail, preview and stream routes.

Security model (it listens on 127.0.0.1 only):
  * the Host header must be 127.0.0.1:<port> or localhost:<port> (blocks DNS rebinding);
  * browser requests must be same-origin (Sec-Fetch-Site) - other web pages cannot talk to it;
  * every /api/* call needs the per-session token that only the served page and the CLI know;
  * media is addressed by database id and only served if the file lies inside an opened folder;
  * served files carry `Content-Security-Policy: sandbox`, so an opened SVG/HTML cannot run script.
"""

import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import APP_NAME, APP_SLUG, AUTHOR, AUTHOR_SITE, LICENSE_NAME, REPO_URL, __version__, convert, db, playback, stats, thumbs, views
from .formats import AUDIO_EXT, IMAGE_EXT, NATIVE_IMAGE, VIDEO_EXT, capabilities, mime_of
from .paths import paths
from .state import GOVERNOR, RUNTIME, STATUS, touch, ui_alive
from .util import ApiError, file_tag, rss_kb, to_int

WEB = Path(__file__).resolve().parent / "web"
TOKEN_HEADER = "X-Heliograph-Token"

CSP_PAGE = ("default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")

# set by service.py
CTX = {"indexer": None, "jobs": None}


def _cache_summary(ttl=10, _c={"t": 0, "v": None}):
    if time.time() - _c["t"] > ttl:
        _c["v"], _c["t"] = thumbs.cache_summary(), time.time()
    return _c["v"]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = APP_SLUG
    timeout = 120

    def log_message(self, fmt, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionError, TimeoutError, socket.timeout):
            pass
        finally:
            db.close_thread_connection()        # one SQLite handle per HTTP connection, closed with it
            thumbs.close_thread_connection()

    # ------------------------------------------------------------------ plumbing
    def _guard(self):
        port = RUNTIME["port"]
        if self.headers.get("Host", "") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            raise ApiError(403, "bad host")                 # DNS-rebinding protection
        if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            raise ApiError(403, "cross-site request")

    def _peer_gone(self):
        return playback._client_closed(self.connection)

    def _send(self, status, body=b"", ctype="application/json; charset=utf-8", extra=None, head=False):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _json(self, obj, status=200, head=False):
        self._send(status, json.dumps(obj, separators=(",", ":")).encode(), extra={"Cache-Control": "no-store"},
                   head=head)

    def _file(self, path, ctype, cache="private, max-age=3600", head=False, sandbox=True):
        try:
            st = os.stat(path)
        except OSError:
            raise ApiError(404, "File not found")
        size = st.st_size
        etag = f'"{st.st_mtime_ns:x}-{size:x}"'
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes=") and size > 0:
            a, _, b = rng[6:].split(",")[0].strip().partition("-")
            try:
                if a == "":
                    start = max(0, size - int(b))
                else:
                    start = int(a)
                    end = min(size - 1, int(b)) if b else size - 1
            except ValueError:
                start, end = 0, size - 1
            else:
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
        elif self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache)
            self.end_headers()
            return
        length = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        if sandbox:
            self.send_header("Content-Security-Policy", "sandbox")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head or length == 0:
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionError, TimeoutError, socket.timeout, OSError):
            self.close_connection = True

    def _bytes(self, data, ctype, etag, cache, head=False):
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache)
            self.end_headers()
            return
        self._send(200, data, ctype, {"ETag": etag, "Cache-Control": cache}, head)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "bad length")
        if n > 4_000_000:
            raise ApiError(413, "too large")
        if n == 0:
            return {}
        try:
            data = json.loads(self.rfile.read(n))
        except ValueError:
            raise ApiError(400, "bad json")
        if not isinstance(data, dict):
            raise ApiError(400, "bad json")
        return data

    def _dispatch(self, method):
        head = method == "HEAD"
        try:
            self._guard()
            u = urllib.parse.urlsplit(self.path)
            path = u.path
            q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
            if (method == "POST" or (path.startswith("/api/") and path != "/api/ping")) and not \
                    secrets.compare_digest(self.headers.get(TOKEN_HEADER, ""), RUNTIME["token"]):
                raise ApiError(403, "bad token")
            if method == "POST":
                touch()
                self._post(path, self._body())
                return
            if path not in ("/api/status", "/api/ping"):
                touch()
            self._get(path, q, head)
        except ApiError as e:
            self._json({"error": e.message}, e.status)
        except Exception:
            traceback.print_exc()
            try:
                self._json({"error": "internal error"}, 500)
            except Exception:
                self.close_connection = True

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_POST(self):
        self._dispatch("POST")

    # ------------------------------------------------------------------ GET routes
    def _get(self, path, q, head):
        if path in ("/", "/index.html"):
            html = (WEB / "index.html").read_text(encoding="utf-8")
            v = str(int(max(f.stat().st_mtime for f in WEB.iterdir() if f.is_file())))
            html = html.replace("__TOKEN__", RUNTIME["token"]).replace("__V__", v)
            self._send(200, html.encode(), "text/html; charset=utf-8",
                       {"Cache-Control": "no-store", "Content-Security-Policy": CSP_PAGE}, head)
            return
        m = re.fullmatch(r"/static/([A-Za-z0-9._-]+)", path)
        if m:
            f = WEB / m.group(1)
            if m.group(1) == "index.html" or not f.is_file():
                raise ApiError(404, "not found")
            ctype = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}.get(
                f.suffix, mimetypes.guess_type(f.name)[0] or "application/octet-stream")
            self._file(f, ctype, cache="no-cache", head=head, sandbox=False)
            return
        m = re.fullmatch(r"/(media|thumb|preview|stream|sub)/(\d{1,18})", path)
        if m:
            self._media(m.group(1), int(m.group(2)), q, head)
            return
        if path == "/api/ping":
            self._json({"app": APP_SLUG, "version": __version__}, head=head)
        elif path == "/api/state":
            self._json(api_state(), head=head)
        elif path == "/api/view":
            self._json(views.api_view(q), head=head)
        elif path == "/api/items":
            self._json(views.api_items(q), head=head)
        elif path == "/api/ids":
            self._json(views.api_ids(q), head=head)
        elif path == "/api/keyframe":
            self._json(api_keyframe(q), head=head)
        elif path == "/api/locate":
            self._json(views.api_locate(q), head=head)
        elif path == "/api/folders":
            self._json(views.api_folders(q), head=head)
        elif path == "/api/years":
            self._json(views.api_years(), head=head)
        elif path == "/api/fs":
            self._json(views.api_fs(q), head=head)
        elif path == "/api/status":
            self._json(api_status(), head=head)
        elif path == "/api/convert/presets":
            self._json(convert.api_presets(), head=head)
        elif path == "/api/jobs":
            self._json(convert.api_jobs(), head=head)
        elif path == "/api/hb":
            RUNTIME["last_ui"] = time.time()
            nav, RUNTIME["nav"] = RUNTIME["nav"], None
            self._json({"v": RUNTIME["version"], "sv": RUNTIME["stats_v"], "scan": STATUS["scan"],
                        "enrich": STATUS["enrich"], "gov": GOVERNOR.snapshot()["state"],
                        "jobs": STATUS["jobs"], "nav": nav}, head=head)
        else:
            m = re.fullmatch(r"/api/item/(\d{1,18})", path)
            if not m:
                raise ApiError(404, "not found")
            self._json(api_item(int(m.group(1))), head=head)

    def _media(self, what, fid, q, head):
        r = views.row_for(fid)
        asked = q.get("k")
        if asked and what in ("media", "thumb", "preview") and asked != file_tag(r["path"], r["mtime"], r["size"]):
            # The page remembers a different file under this id (ids are reused after a folder is removed), or the file
            # changed on disk. Say so, rather than send a picture that belongs to something else.
            raise ApiError(404, "This file changed - reload")
        if what == "media":
            self._file(r["path"], mime_of(r["name"]), head=head)
        elif what == "thumb":
            if r["ext"] == "svg":
                self._file(r["path"], "image/svg+xml", head=head)
                return
            state, data = thumbs.get_thumb(r, thumbs.THUMB_PX, alive=lambda: not self._peer_gone())
            if state == "gone":
                self.close_connection = True
                return
            if state != "ok":
                raise ApiError(404, "no preview")
            etag = '"t%x"' % (thumbs.key_for(r, thumbs.THUMB_PX)[0] & 0xFFFFFFFFFFFF)
            self._bytes(data, "image/jpeg", etag, "private, max-age=31536000, immutable", head)
        elif what == "preview":
            p = thumbs.preview_path(r, alive=lambda: not self._peer_gone())
            if p is None:
                if self._peer_gone():
                    self.close_connection = True
                    return
                raise ApiError(404, "no preview")
            self._file(p, "image/jpeg", cache="private, max-age=31536000, immutable", head=head)
        elif what == "stream":
            self._stream(r, q)
        elif what == "sub":
            self._subtitle(r, q, head)

    def _stream(self, r, q):
        info = playback.probe(r)
        if not info:
            raise ApiError(415, "This file cannot be read")
        plan = playback.decide(info, r["kind"])
        mode = q.get("mode") or plan["mode"]
        if mode == "direct":
            mode = "remux"
        if mode not in ("remux", "transcode"):
            raise ApiError(400, "bad mode")
        try:
            start = max(0.0, float(q.get("start", 0)))
        except ValueError:
            start = 0.0
        try:
            audio = to_int(q["audio"], "audio track") if q.get("audio") not in (None, "") else None
        except ValueError:
            audio = None
        try:
            max_h = to_int(q.get("h") or db.get_setting("transcode_max_height", "1080"), "height")
        except ValueError:
            max_h = 1080
        gpu = db.get_setting("transcode_gpu", "auto")
        cmds = playback.candidates(r["path"], info, mode, start, audio, max_h, gpu)
        res = playback.stream_to(self, cmds, label=r["name"])
        if res == -1:
            raise ApiError(500, "Playback conversion failed - see the service log")
        if res < -1:
            self.close_connection = True

    def _subtitle(self, r, q, head):
        exe = shutil.which("ffmpeg")
        if not exe:
            raise ApiError(404, "ffmpeg is not installed")
        info = playback.probe(r) or {}
        try:
            start = max(0.0, float(q.get("start", 0)))
        except ValueError:
            start = 0.0
        seek = ["-ss", f"{start:.3f}"] if start else []
        if q.get("f"):
            names = {s.get("file") for s in info.get("subs", []) if s.get("file")}
            if q["f"] not in names:
                raise ApiError(404, "No such subtitle")
            cmd = [exe, "-nostdin", "-v", "error", *seek, "-i", os.path.join(r["dir"], q["f"]), "-f", "webvtt", "pipe:1"]
        else:
            try:
                idx = int(q.get("i", ""))
            except ValueError:
                raise ApiError(400, "bad stream")
            if idx not in {s.get("index") for s in info.get("subs", []) if s.get("text")}:
                raise ApiError(404, "No such subtitle")
            cmd = [exe, "-nostdin", "-v", "error", *seek, "-i", r["path"], "-map", f"0:{idx}", "-f", "webvtt", "pipe:1"]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            raise ApiError(500, "Could not read the subtitles")
        if p.returncode != 0 or not p.stdout:
            raise ApiError(404, "No subtitle text")
        self._send(200, p.stdout, "text/vtt; charset=utf-8", {"Cache-Control": "no-store"}, head)

    # ------------------------------------------------------------------ POST routes
    def _post(self, path, b):
        idx = CTX["indexer"]
        if path == "/api/roots":
            self._json(views.open_folder(idx, str(b.get("path", "")), persist=True))
        elif path == "/api/open":
            p = os.path.abspath(os.path.expanduser(str(b.get("path", ""))))
            if os.path.isdir(p):
                self._json(views.open_folder(idx, p, persist=bool(b.get("add"))))
            else:
                self._json(views.open_file(idx, p))
        elif path == "/api/roots/remove":
            self._json(views.remove_root(to_int(b.get("id"), "id", 0)))
        elif path == "/api/rescan":
            idx.request_scan(to_int(b["id"], "id") if b.get("id") else None)
            self._json({"ok": True})
        elif path == "/api/fav":
            self._json(views.set_fav(to_int(b.get("id"), "id", 0), bool(b.get("value"))))
        elif path == "/api/open_external":
            self._json(views.do_open(to_int(b.get("id"), "id", 0), str(b.get("action", ""))))
        elif path == "/api/navigate":
            RUNTIME["nav_n"] += 1
            RUNTIME["nav"] = {"n": RUNTIME["nav_n"], "dir": str(b.get("dir", "")), "file": b.get("file")}
            self._json({"ok": True})
        elif path == "/api/want":
            ids = [int(i) for i in (b.get("ids") or [])[:200] if isinstance(i, int)]
            idx.want_ids(ids)
            self._json({"ok": True})
        elif path == "/api/convert":
            self._json(api_convert(b))
        elif path == "/api/jobs/cancel":
            CTX["jobs"].cancel(to_int(b.get("id"), "id", 0))
            self._json({"ok": True})
        elif path == "/api/jobs/clear":
            convert.clear_finished()
            self._json({"ok": True})
        elif path == "/api/settings":
            self._json(api_settings(b))
        elif path == "/api/cache":
            self._json(api_cache(str(b.get("action", ""))))
        else:
            raise ApiError(404, "not found")


# --------------------------------------------------------------------------- API implementations

def api_state():
    c = db.conn()
    hw = playback.hardware()
    return {"app": APP_NAME, "version": __version__, "home": str(Path.home()), "v": RUNTIME["version"],
            "roots": views.api_roots(), "totals": stats.totals(c),
            "tools": dict(capabilities(), dolphin=bool(shutil.which("dolphin"))),
            "gpu": bool(hw["device"] and hw["encode_h264"]), "settings": db.all_settings(),
            "native_image": sorted(NATIVE_IMAGE),
            "about": {"author": AUTHOR, "site": AUTHOR_SITE, "repo": REPO_URL, "license": LICENSE_NAME,
                      "formats": {"image": len(IMAGE_EXT), "video": len(VIDEO_EXT), "audio": len(AUDIO_EXT)}}}


def api_item(fid):
    d = views.api_item(fid)
    r = views.row_for(fid)
    if d["kind"] == "image":
        d["native"] = d["ext"] in NATIVE_IMAGE
    else:
        info = playback.probe(r)
        d["info"] = info
        d["play"] = playback.decide(info, d["kind"])
        d["mime"] = mime_of(d["name"])
    return d


def api_keyframe(q):
    r = views.row_for(to_int(q.get("id"), "id", 0))
    try:
        t = max(0.0, float(q.get("t", 0)))
    except ValueError:
        raise ApiError(400, "bad time")
    return {"start": round(playback.keyframe_at(r["path"], t), 3)}


def api_status():
    c = db.conn()
    n = c.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    busy = STATUS["scan"]["state"] == "scanning" or STATUS["jobs"]["running"] or STATUS["stream"]["active"]
    idle_in = None if busy else max(0, int(RUNTIME["idle_exit"] - (time.time() - RUNTIME["last_activity"])))
    p = paths()
    th = dict(STATUS["thumbs"])
    cs = _cache_summary()
    return {"app": APP_SLUG, "version": __version__, "pid": os.getpid(), "uptime": int(time.time() - RUNTIME["started"]),
            "rss_kb": rss_kb(), "files": n, "scan": STATUS["scan"], "enrich": STATUS["enrich"], "thumbs": th,
            "jobs": STATUS["jobs"], "stream": STATUS["stream"], "gov": GOVERNOR.snapshot(),
            "ui_alive": ui_alive(), "nav_pending": RUNTIME["nav"] is not None, "idle_exit_in": idle_in,
            "roots": [{"path": r["path"], "online": r["online"], "persist": r["persist"]} for r in views.api_roots()],
            "db": str(p.db), "cache": str(p.thumbs_db), "cache_info": cs,
            "db_bytes": p.db.stat().st_size if p.db.exists() else 0, "now": time.time()}


def api_convert(b):
    ids = b.get("ids")
    if ids is None and b.get("view"):
        ids = views.api_ids({"view": b["view"], "from": b.get("from", 0), "to": b.get("to", 0)})["ids"]
    if not isinstance(ids, list) or not ids:
        raise ApiError(400, "Nothing selected")
    if len(ids) > 20000:
        raise ApiError(400, "Select at most 20,000 files at once")
    rows = []
    for i in ids:
        try:
            rows.append(views.row_for(to_int(i, "id")))
        except ApiError as e:
            if e.status == 400:
                raise
            continue                                    # a file that vanished since the selection was made
    if not rows:
        raise ApiError(404, "None of the selected files exist any more")
    spec = {k: b.get(k) for k in ("preset", "quality", "max_height", "max_size", "gpu", "strip", "out_dir")
            if b.get(k) not in (None, "")}
    preset = convert.PRESETS.get(spec.get("preset"))
    if not preset:
        raise ApiError(400, "Unknown conversion preset")
    fit = [r for r in rows if r["kind"] in preset[2]]          # a mixed selection converts what fits
    if not fit:
        raise ApiError(400, f"{preset[0]} cannot be made from the selected files")
    new = CTX["jobs"].submit(fit, spec)
    return {"jobs": new, "skipped": len(ids) - len(fit)}


def api_settings(b):
    try:
        clean = {k: db.validate_setting(k, v) for k, v in b.items()}      # all or nothing
    except ValueError as e:
        raise ApiError(400, str(e))
    for k, v in clean.items():
        db.set_setting(k, v)
    CTX["indexer"].wake.set()
    return db.all_settings()


def api_cache(action):
    if action == "clear":
        n = thumbs.clear()
        _cache_summary(ttl=0)
        return {"cleared": n}
    if action == "prune":
        return {"pruned": thumbs.prune()}
    raise ApiError(400, "unknown action")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError, socket.timeout)):
            traceback.print_exc()
