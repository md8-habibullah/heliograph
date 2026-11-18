"""Desktop integration: launcher entry, icon, and (only when asked) default-application settings.

`heliograph install` writes a .desktop file and the icon under ~/.local/share, so Heliograph shows up in
the app menu and in "Open with" for pictures, videos, audio and folders. It never changes which
application is the default. `heliograph default` does that, explicitly, and remembers the previous
choices so `heliograph default --restore` can undo it.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import APP_ID, APP_NAME
from .formats import mime_types_for_desktop_file
from .paths import paths

WEB = Path(__file__).resolve().parent / "web"
DESKTOP_NAME = f"{APP_ID}.desktop"

DEFAULT_GROUPS = {
    "images": ["image/jpeg", "image/png", "image/gif", "image/webp", "image/avif", "image/bmp", "image/tiff",
               "image/heic", "image/heif", "image/jxl", "image/x-icon", "image/jp2"],
    "videos": ["video/mp4", "video/quicktime", "video/x-matroska", "video/webm", "video/x-msvideo",
               "video/x-ms-wmv", "video/x-flv", "video/mpeg", "video/mp2t", "video/3gpp", "video/ogg",
               "video/x-m4v"],
    "audio": ["audio/mpeg", "audio/flac", "audio/x-flac", "audio/mp4", "audio/x-m4a", "audio/aac", "audio/ogg",
              "audio/opus", "audio/wav", "audio/x-wav", "audio/webm", "audio/x-ms-wma", "audio/x-aiff"],
    "folders": ["inode/directory"],
}


def launcher_path():
    """Absolute path of the command that starts Heliograph (for the Exec= line)."""
    exe = shutil.which("heliograph")
    return exe or str(Path(sys.argv[0]).resolve())


def applications_dir():
    base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local/share"))
    return base / "applications"


def icon_dir():
    base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local/share"))
    return base / "icons/hicolor/scalable/apps"


def desktop_entry(exe, wm_class=None):
    mimes = ";".join(mime_types_for_desktop_file()) + ";"
    return f"""[Desktop Entry]
Type=Application
Version=1.0
Name={APP_NAME}
GenericName=Media Library and Player
Comment=Fast, private library and player for your pictures, videos and music
Exec={exe} %F
TryExec={exe}
Icon={APP_ID}
Terminal=false
Categories=AudioVideo;Video;Audio;Player;
MimeType={mimes}
StartupNotify=false
StartupWMClass={wm_class or APP_ID}
Keywords=media;library;photos;pictures;gallery;video;music;audio;player;viewer;convert;ffmpeg;
"""


def install(exe=None, wm_class=None):
    from .window import window_class
    exe = exe or launcher_path()
    wm_class = wm_class or window_class()
    ap, ic = applications_dir(), icon_dir()
    ap.mkdir(parents=True, exist_ok=True)
    ic.mkdir(parents=True, exist_ok=True)
    desktop = ap / DESKTOP_NAME
    desktop.write_text(desktop_entry(exe, wm_class), encoding="utf-8")
    shutil.copyfile(WEB / "icon.svg", ic / f"{APP_ID}.svg")
    for tool, args in (("update-desktop-database", [str(ap)]),
                       ("gtk-update-icon-cache", ["-q", "-t", str(ic.parents[2])])):
        if shutil.which(tool):
            subprocess.run([tool, *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"desktop": str(desktop), "icon": str(ic / f"{APP_ID}.svg"), "exec": exe, "wm_class": wm_class}


def uninstall():
    removed = []
    for f in (applications_dir() / DESKTOP_NAME, icon_dir() / f"{APP_ID}.svg"):
        if f.exists():
            f.unlink()
            removed.append(str(f))
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(applications_dir())], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    return removed


# --------------------------------------------------------------------------- defaults

def _query_default(mime):
    try:
        p = subprocess.run(["xdg-mime", "query", "default", mime], capture_output=True, text=True, timeout=5)
        return p.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _saved_path():
    return paths().state / "previous-defaults.json"


def set_defaults(groups):
    """Make Heliograph the default for the chosen groups; remembers what was there before."""
    if not shutil.which("xdg-mime"):
        raise RuntimeError("xdg-mime is not installed")
    saved = {}
    try:
        saved = json.loads(_saved_path().read_text())
    except (OSError, ValueError):
        pass
    changed = []
    for g in groups:
        for mime in DEFAULT_GROUPS[g]:
            cur = _query_default(mime)
            if cur != DESKTOP_NAME:
                saved.setdefault(mime, cur)                # keep the *original* value on repeated runs
                subprocess.run(["xdg-mime", "default", DESKTOP_NAME, mime], check=False)
                changed.append(mime)
    paths().ensure()
    _saved_path().write_text(json.dumps(saved, indent=1))
    return changed


def _remove_default(mime):
    """Delete our own `mime=heliograph.desktop` line from the user's mimeapps.list.

    xdg-mime can set a default but has no command to clear one, so a type that had no default before we
    took it over is put back by removing our line (and only exactly our line)."""
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    f = base / "mimeapps.list"
    try:
        lines = f.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return False
    out, section, removed = [], "", False
    for line in lines:
        st = line.strip()
        if st.startswith("[") and st.endswith("]"):
            section = st
        if section == "[Default Applications]" and st == f"{mime}={DESKTOP_NAME}":
            removed = True
            continue
        out.append(line)
    if removed:
        tmp = f.with_name(f.name + ".heliograph-tmp")
        tmp.write_text("".join(out), encoding="utf-8")
        os.replace(tmp, f)
    return removed


def restore_defaults():
    try:
        saved = json.loads(_saved_path().read_text())
    except (OSError, ValueError):
        return []
    restored = []
    for mime, prev in saved.items():
        if _query_default(mime) != DESKTOP_NAME:
            continue
        if prev:
            subprocess.run(["xdg-mime", "default", prev, mime], check=False)
        else:
            _remove_default(mime)
        restored.append(mime)
    _saved_path().unlink(missing_ok=True)
    return restored


def default_status():
    out = {}
    for g, mimes in DEFAULT_GROUPS.items():
        out[g] = {m: _query_default(m) for m in mimes[:3]}
    return out
