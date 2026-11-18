"""Process-wide runtime state shared by the service threads (status for the UI and the monitor)."""

import threading
import time

from . import db
from .governor import Governor

STATUS = {
    "scan": {"state": "idle", "root": None, "files": 0, "dirs": 0, "started": None,
             "finished": None, "last_files": 0, "last_seconds": 0.0},
    "enrich": {"state": "idle", "done": 0, "remaining": 0, "started": None, "reason": "", "current": None},
    "thumbs": {"made": 0, "failed": 0, "cached": 0, "inflight": 0, "queued": 0, "ms_total": 0},
    "jobs": {"running": 0, "queued": 0},
    "stream": {"active": 0},
}
RUNTIME = {"version": 1, "stats_v": 1, "last_activity": time.time(), "last_ui": 0.0, "nav": None,
           "nav_n": 0, "started": time.time(), "port": 0, "token": "", "idle_exit": 15 * 60}
LOCK = threading.Lock()
USER_UMASK = [0o022]                  # the umask the user started with; the service itself runs with 077

GOVERNOR = Governor(get_setting=db.get_setting)


def bump_version():
    """The set of files changed (scan finished a batch, folder added/removed, conversion done)."""
    RUNTIME["version"] += 1
    RUNTIME["stats_v"] += 1


def bump_stats():
    """Only aggregates changed (dates read from files): refresh the sidebar, not the grid."""
    RUNTIME["stats_v"] += 1


def touch():
    RUNTIME["last_activity"] = time.time()


def ui_alive(window=12):
    return time.time() - RUNTIME["last_ui"] < window
