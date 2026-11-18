"""The application window: a Chromium-based browser in app mode (no tabs, no address bar)."""

import os
import shutil
import subprocess

from . import APP_ID, APP_NAME
from .util import spawn

BROWSERS = ("brave-origin", "brave", "brave-browser", "chromium", "chromium-browser", "google-chrome-stable",
            "google-chrome", "chrome", "microsoft-edge-stable", "vivaldi")


def find_browser():
    custom = os.environ.get("HELIOGRAPH_BROWSER")
    if custom:
        return custom.split()
    for exe in BROWSERS:
        p = shutil.which(exe)
        if p:
            return [p]
    return None


# On Wayland a Chromium-based browser ignores --class and names an app window itself:
# "<product>-<host><path with / as _>-<profile>", e.g. "brave-127.0.0.1__-Default" (checked on Brave and Chromium).
# The port is not part of the name. On X11 --class works and the name is simply APP_ID.
WAYLAND_PRODUCT = (("brave", "brave"), ("chromium", "chrome"), ("google-chrome", "chrome"), ("chrome", "chrome"),
                   ("microsoft-edge", "msedge"), ("vivaldi", "vivaldi"))


def wayland_class(browser_cmd, profile="Default"):
    """The app id a Chromium-based browser gives our window on Wayland, or None if we do not know that browser."""
    name = os.path.basename(browser_cmd[0])
    for prefix, product in WAYLAND_PRODUCT:
        if name.startswith(prefix):
            return f"{product}-127.0.0.1__-{profile}"
    return None


def window_class():
    """Best guess of the class / app id our window will have (used for StartupWMClass in the launcher)."""
    if os.environ.get("XDG_SESSION_TYPE") == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
        browser = find_browser()
        if browser:
            return wayland_class(browser) or APP_ID
    return APP_ID


def open_window(url, tab=False):
    browser = None if tab else find_browser()
    if browser:
        return spawn(browser + [f"--app={url}", f"--class={APP_ID}"])
    return spawn(["xdg-open", url])


def focus_window():
    """Bring an already-open app window to the front (Hyprland only; harmless elsewhere)."""
    if not shutil.which("hyprctl"):
        return
    # Hyprland >= 0.55 takes Lua dispatchers; older versions take "focuswindow <selector>".
    # Selectors must match the whole title, hence the leading ".*".
    attempts = ([f'hl.dsp.focus({{ window = "title:.*{APP_NAME}" }})'], ["focuswindow", f"title:.*{APP_NAME}"])
    for args in attempts:
        try:
            p = subprocess.run(["hyprctl", "dispatch", *args], capture_output=True, text=True, timeout=3)
            out = p.stdout + p.stderr
        except (OSError, subprocess.TimeoutExpired):
            return
        if "error" not in out.lower() and "not found" not in out.lower():
            return
