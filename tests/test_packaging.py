"""Packaging files stay in step with the code, and are valid."""

import re
import shutil
import subprocess
import unittest
import xml.etree.ElementTree as ET

from helpers import ROOT, SRC  # noqa: F401  (sets up sys.path)

from heliograph import APP_ID, __version__, install

PKG = ROOT / "packaging"


class PackagingTest(unittest.TestCase):
    def test_packaged_desktop_file_is_what_install_would_write(self):
        self.assertEqual((PKG / f"{APP_ID}.desktop").read_text(), install.desktop_entry("heliograph"),
                         "regenerate it: python3 -c \"import sys; sys.path.insert(0,'src'); from heliograph import install; "
                         "open('packaging/dev.habibullah.Heliograph.desktop','w').write(install.desktop_entry('heliograph'))\"")

    @unittest.skipUnless(shutil.which("desktop-file-validate"), "needs desktop-file-validate")
    def test_desktop_file_validates_without_hints(self):
        r = subprocess.run(["desktop-file-validate", str(PKG / f"{APP_ID}.desktop")], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout + r.stderr), (0, ""))

    def test_metainfo_matches_the_project(self):
        root = ET.parse(PKG / f"{APP_ID}.metainfo.xml").getroot()
        self.assertEqual(root.findtext("id"), APP_ID)
        self.assertEqual(root.findtext("project_license"), "MIT")
        self.assertEqual(root.find("releases/release").get("version"), __version__)
        self.assertEqual(root.find("launchable").text, f"{APP_ID}.desktop")

    @unittest.skipUnless(shutil.which("appstreamcli"), "needs appstreamcli")
    def test_metainfo_validates(self):
        r = subprocess.run(["appstreamcli", "validate", "--no-net", str(PKG / f"{APP_ID}.metainfo.xml")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_versions_agree(self):
        text = (ROOT / "pyproject.toml").read_text()
        self.assertEqual(re.search(r'^version = "([^"]+)"', text, re.M).group(1), __version__)
        self.assertEqual(re.search(r"^pkgver=(\S+)", (PKG / "PKGBUILD").read_text(), re.M).group(1), __version__)
        self.assertIn(f"## [{__version__}]", (ROOT / "CHANGELOG.md").read_text())

    def test_window_class_for_wayland_and_x11(self):
        from unittest import mock

        from heliograph import window
        self.assertEqual(window.wayland_class(["/usr/bin/brave-origin"]), "brave-127.0.0.1__-Default")
        self.assertEqual(window.wayland_class(["/usr/bin/chromium"]), "chrome-127.0.0.1__-Default")
        self.assertEqual(window.wayland_class(["/usr/bin/google-chrome-stable"]), "chrome-127.0.0.1__-Default")
        self.assertIsNone(window.wayland_class(["/usr/bin/some-other-browser"]))
        with mock.patch.dict("os.environ", {"XDG_SESSION_TYPE": "x11"}, clear=False), \
                mock.patch.dict("os.environ", {"WAYLAND_DISPLAY": ""}, clear=False):
            self.assertEqual(window.window_class(), APP_ID, "X11 honours --class")
        with mock.patch.dict("os.environ", {"XDG_SESSION_TYPE": "wayland"}), \
                mock.patch.object(window, "find_browser", return_value=["/usr/bin/brave"]):
            self.assertEqual(window.window_class(), "brave-127.0.0.1__-Default")
        with mock.patch.dict("os.environ", {"XDG_SESSION_TYPE": "wayland"}), \
                mock.patch.object(window, "find_browser", return_value=["/opt/unknown"]):
            self.assertEqual(window.window_class(), APP_ID, "unknown browser: fall back to our own id")

    def test_launcher_uses_the_given_window_class(self):
        self.assertIn("StartupWMClass=brave-127.0.0.1__-Default\n", install.desktop_entry("heliograph", "brave-127.0.0.1__-Default"))
        self.assertIn(f"StartupWMClass={APP_ID}\n", install.desktop_entry("heliograph"))

    def test_icon_is_the_one_the_app_serves(self):
        self.assertEqual((PKG / f"{APP_ID}.svg").read_text(), (SRC / "heliograph" / "web" / "icon.svg").read_text())


if __name__ == "__main__":
    unittest.main()
