"""Shared test helpers. Every test gets its own HELIOGRAPH_HOME and its own library folder, so tests never touch
the real library, and never need pytest (plain `python -m unittest` is enough)."""

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from heliograph import db  # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
try:
    import PIL  # noqa: F401
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


class IsolatedHome(unittest.TestCase):
    """A fresh private HELIOGRAPH_HOME plus an empty library folder (self.lib) for each test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="heliograph-test-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        self.home, self.lib = base / "home", base / "library"
        self.lib.mkdir()
        self._old_home = os.environ.get("HELIOGRAPH_HOME")
        os.environ["HELIOGRAPH_HOME"] = str(self.home)
        self.addCleanup(self._restore)
        db.init()
        from heliograph import views
        views.clear_cache()                       # snapshots are keyed by the library version, not by database

    def _restore(self):
        from heliograph import thumbs
        db.close_thread_connection()
        thumbs.close_thread_connection()
        if self._old_home is None:
            os.environ.pop("HELIOGRAPH_HOME", None)
        else:
            os.environ["HELIOGRAPH_HOME"] = self._old_home

    def touch(self, rel, data=b"x", mtime=None):
        """Create a file below the library folder (parents included) and return its path."""
        p = self.lib / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def index(self, persist=True, path=None):
        """Register the library folder as a root and scan it synchronously (no background threads)."""
        from heliograph import views
        from heliograph.indexer import Indexer
        ix = Indexer()
        res = views.open_folder(ix, str(path or self.lib), persist=persist)
        ix.scan(res["root_id"])
        ix.pending.clear()
        return ix, res["root_id"]


def tiny_png(path, size=(64, 48), color=(200, 40, 40)):
    from PIL import Image
    Image.new("RGB", size, color).save(path)
    return path


def wait_for(cond, timeout=20.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    return None
