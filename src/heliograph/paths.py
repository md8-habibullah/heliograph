"""Where Heliograph keeps its files (XDG layout, overridable for tests with HELIOGRAPH_HOME)."""

import os
from pathlib import Path

from . import APP_SLUG


class Paths:
    def __init__(self):
        override = os.environ.get("HELIOGRAPH_HOME")
        if override:
            base = Path(override).expanduser()
            self.data, self.state, self.cache = base / "data", base / "state", base / "cache"
        else:
            self.data = self._xdg("XDG_DATA_HOME", ".local/share") / APP_SLUG
            self.state = self._xdg("XDG_STATE_HOME", ".local/state") / APP_SLUG
            self.cache = self._xdg("XDG_CACHE_HOME", ".cache") / APP_SLUG
        self.db = self.data / "heliograph.db"           # roots, files, favourites, settings, jobs
        self.user_json = self.data / "user-data.json"   # tiny snapshot of what cannot be rebuilt
        self.thumbs_db = self.cache / "thumbs.db"       # thumbnail blobs (a cache, safe to delete)
        self.previews = self.cache / "previews"         # large converted previews (files, LRU pruned)
        self.info = self.state / "server.json"          # pid / port / token of the running service
        self.log = self.state / "server.log"

    @staticmethod
    def _xdg(var, default):
        return Path(os.environ.get(var) or (Path.home() / default))

    def ensure(self):
        """Create the private directories (0700). Safe to call repeatedly."""
        for d in (self.data, self.state, self.cache, self.previews):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(d, 0o700)
            except OSError:
                pass


def paths():
    return Paths()
