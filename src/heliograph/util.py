"""Small shared helpers: natural sort keys, date parsing, low-priority subprocesses."""

import datetime
import hashlib
import os
import re
import shutil
import subprocess
import threading
import time

_NUM = re.compile(r"\d+")
_SHOT = re.compile(r"screen[\s_.-]?(?:shot|cap|grab|rec)", re.I)
_DT = re.compile(r"(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")


def file_tag(path, mtime, size):
    """A short identity for one file as it is now: it changes with the path, the size or the modified time.

    File ids are only unique while a file stays in the index: when a folder is removed or the service restarts, its ids
    are handed to other files. Anything cached under an id (a thumbnail in the page, in the browser's HTTP cache)
    must therefore also carry this tag, so that a new file never picks up the picture of an old one."""
    return hashlib.sha1(f"{path}\0{mtime}\0{size}".encode()).hexdigest()[:12]


def to_int(value, what="value", default=None):
    """A request parameter as an int; anything else is a clean 400 instead of a crash (bool and floats are refused)."""
    if value is None or value == "":
        if default is not None:
            return default
        raise ApiError(400, f"Missing {what}")
    if isinstance(value, bool) or isinstance(value, float):
        raise ApiError(400, f"Bad {what}")
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, f"Bad {what}") from None
    if not -2**62 < n < 2**62:                          # larger than SQLite can hold
        raise ApiError(400, f"Bad {what}")
    return n


class ApiError(Exception):
    """An error that maps to an HTTP status and a message that is safe to show the user."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def natkey(s):
    """Sort key where IMG_2 sorts before IMG_10."""
    return _NUM.sub(lambda m: m.group().zfill(12), s.lower())


def is_screenshot(name, folder):
    return bool(_SHOT.search(name)) or "screenshot" in folder.lower()


def parse_dt(value, utc=False):
    """'2021:05:03 10:11:12' / ISO strings -> unix time, or None if missing or implausible."""
    if not value:
        return None
    if isinstance(value, bytes):
        value = value.decode("ascii", "ignore")
    m = _DT.match(str(value).strip().strip("\x00"))
    if not m:
        return None
    try:
        dt = datetime.datetime(*map(int, m.groups()))
    except ValueError:
        return None
    if not (1990 <= dt.year <= datetime.date.today().year + 1):
        return None
    try:
        if utc:
            return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())
        return int(dt.timestamp())
    except (OverflowError, OSError, ValueError):
        return None


def local_ts(y, m, d):
    return int(time.mktime((y, m, d, 0, 0, 0, 0, 0, -1)))


def within(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def subtree_sql(col="dir"):
    """SQL fragment matching a folder and everything below it (uses the index on dir)."""
    return f"({col}=? OR ({col}>=? AND {col}<?))"


def subtree_args(d):
    return [d, d + "/", d + "0"]            # '0' is the character right after '/'


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_duration(sec):
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m"


# --------------------------------------------------------------------------- priorities

_LOW_PREFIX = None


def low_prefix(nice=10):
    """Command prefix that runs a helper at low CPU and I/O priority (when nice/ionice exist)."""
    global _LOW_PREFIX
    if _LOW_PREFIX is None:
        p = []
        if shutil.which("nice"):
            p += ["nice", "-n", "{nice}"]
        if shutil.which("ionice"):
            p += ["ionice", "-c", "2", "-n", "7"]
        _LOW_PREFIX = p
    return [x.replace("{nice}", str(nice)) for x in _LOW_PREFIX]


def run_low(cmd, timeout, nice=10, input_bytes=None):
    """Run a helper at low priority; returns (returncode, stdout bytes). Never raises."""
    try:
        p = subprocess.run(low_prefix(nice) + list(cmd), input=input_bytes,
                           stdin=subprocess.DEVNULL if input_bytes is None else None,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
        return p.returncode, p.stdout
    except (subprocess.TimeoutExpired, OSError):
        return 1, b""


def spawn(cmd):
    """Start a desktop program detached from the service."""
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return False


def lower_this_thread(nice=10):
    """Lower the CPU priority of the calling thread only (Linux: nice is per thread)."""
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), nice)
    except (OSError, AttributeError):
        pass


def lower_process_io():
    """Best-effort: put this whole process in the 'best effort, low' I/O class."""
    if shutil.which("ionice"):
        try:
            subprocess.run(["ionice", "-c", "2", "-n", "6", "-p", str(os.getpid())],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass


def rss_kb():
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0
