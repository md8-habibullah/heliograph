"""The background service: starts the HTTP server, the indexer and the conversion queue."""

import atexit
import json
import os
import secrets
import signal
import sys
import threading
import time

from . import __version__, convert, db, playback, thumbs, views
from .indexer import Indexer
from .paths import paths
from .server import CTX, Server, Handler
from .state import GOVERNOR, RUNTIME, STATUS, USER_UMASK, touch
from .util import lower_process_io

PREFERRED_PORT = 47183


def serve(port=PREFERRED_PORT, idle_exit=None):
    from .client import server_info
    if server_info():
        print("heliograph service is already running", file=sys.stderr)
        return 1
    USER_UMASK[0] = os.umask(0o077)                  # database, thumbnails and state are private to this user
    p = paths()
    p.ensure()
    try:
        os.nice(5)                                   # the whole service is a polite background citizen
    except OSError:
        pass
    lower_process_io()
    crashed = p.info.exists()                        # a stale server.json means the last run did not exit cleanly
    status = db.init(check=crashed)
    if status != "ok":
        print(f"heliograph: database {status}; library folders and favourites were restored from the snapshot",
              flush=True)
    views.purge_session_roots()
    if idle_exit is not None:
        RUNTIME["idle_exit"] = idle_exit
    else:
        try:
            RUNTIME["idle_exit"] = int(db.get_setting("idle_exit_min", "15")) * 60
        except ValueError:
            RUNTIME["idle_exit"] = 900

    httpd = None
    for pt in (port, 0):
        try:
            httpd = Server(("127.0.0.1", pt), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        print("could not open a local port", file=sys.stderr)
        return 1
    RUNTIME["port"] = httpd.server_address[1]
    RUNTIME["token"] = secrets.token_urlsafe(24)
    info = {"pid": os.getpid(), "port": RUNTIME["port"], "token": RUNTIME["token"], "started": time.time(),
            "version": __version__}
    tmp = p.info.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(info, f)
    os.replace(tmp, p.info)

    indexer = Indexer()
    jobs = convert.JobManager(on_done=lambda dst: _after_conversion(indexer, dst))
    CTX["indexer"], CTX["jobs"] = indexer, jobs

    def cleanup():
        try:
            if json.loads(p.info.read_text()).get("pid") == os.getpid():
                p.info.unlink()
        except (OSError, ValueError):
            pass

    atexit.register(cleanup)
    indexer.start()
    jobs.start()
    indexer.request_scan(None, min_age=30)
    db.write_user_data()
    threading.Thread(target=playback.hardware, daemon=True).start()       # detect VAAPI once, off the hot path
    threading.Thread(target=thumbs.prune, daemon=True).start()
    threading.Thread(target=thumbs.prune_previews, daemon=True).start()

    stopping = threading.Event()

    def stop(*_):
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def watchdog():
        while not stopping.wait(20):
            try:
                thumbs.flush_touch()
            except Exception:
                pass
            busy = (STATUS["scan"]["state"] == "scanning" or STATUS["jobs"]["running"] or STATUS["jobs"]["queued"]
                    or STATUS["stream"]["active"])
            if not busy and time.time() - RUNTIME["last_activity"] > RUNTIME["idle_exit"]:
                stop()
                return

    threading.Thread(target=watchdog, daemon=True).start()
    touch()
    print(f"heliograph {__version__} listening on http://127.0.0.1:{RUNTIME['port']} (pid {os.getpid()})", flush=True)
    try:
        httpd.serve_forever()
    finally:
        playback.stop_all_streams()
        jobs.stop()
        indexer.stop()
        try:
            thumbs.flush_touch()
        except Exception:
            pass
        db.write_user_data()
    return 0


def _after_conversion(indexer, dst):
    """A converted file is new: re-scan the opened folder it landed in so it shows up."""
    d = os.path.dirname(dst)
    c = db.conn()
    best = None
    for r in c.execute("SELECT id, path, depth FROM roots"):
        if (d == r["path"] or d.startswith(r["path"].rstrip("/") + "/")) and (best is None or len(r["path"]) > len(best["path"])):
            best = r
    if best is not None:
        indexer.request_scan(best["id"])


__all__ = ["serve", "PREFERRED_PORT", "GOVERNOR"]
