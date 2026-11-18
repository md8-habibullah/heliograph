"""Launcher installation and default-application handling, always inside a private XDG home."""

import os
import shutil
import unittest
from unittest import mock

from helpers import IsolatedHome

from heliograph import install

MIME_NEW = "application/x-heliograph-test-nodefault"     # a type nobody has a default for
MIME_OLD = "application/x-heliograph-test-hasdefault"


class XdgHome(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.xdg_config = self.home.parent / "xdg-config"
        self.xdg_data = self.home.parent / "xdg-data"
        (self.xdg_config).mkdir()
        (self.xdg_data / "applications").mkdir(parents=True)
        # Keep the system data dirs: without the shared MIME database `xdg-mime query` answers nothing at all.
        system_data = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
        env = {"XDG_CONFIG_HOME": str(self.xdg_config), "XDG_DATA_HOME": str(self.xdg_data),
               "XDG_CURRENT_DESKTOP": "", "XDG_DATA_DIRS": f"{self.xdg_data}:{system_data}"}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)

    def mimeapps(self):
        f = self.xdg_config / "mimeapps.list"
        return f.read_text() if f.exists() else ""


class InstallTest(XdgHome):
    def test_install_and_uninstall_touch_only_our_two_files(self):
        r = install.install("/usr/bin/heliograph", "my-window-class")
        desktop = self.xdg_data / "applications" / install.DESKTOP_NAME
        self.assertEqual(r["desktop"], str(desktop))
        self.assertIn("Exec=/usr/bin/heliograph %F", desktop.read_text())
        self.assertIn("StartupWMClass=my-window-class", desktop.read_text())
        self.assertTrue((self.xdg_data / "icons/hicolor/scalable/apps/dev.habibullah.Heliograph.svg").exists())
        self.assertEqual(self.mimeapps(), "", "install must not change any default")
        self.assertEqual(len(install.uninstall()), 2)
        self.assertFalse(desktop.exists())


@unittest.skipUnless(shutil.which("xdg-mime"), "needs xdg-mime")
class DefaultsTest(XdgHome):
    def setUp(self):
        super().setUp()
        # xdg-mime skips a launcher whose Exec binary is not a real file, so point both launchers at one.
        self.binary = shutil.which("true") and os.path.realpath(shutil.which("true"))
        install.install(self.binary)
        # xdg-mime ignores a default whose launcher does not declare the type, so declare the made-up ones.
        desktop = self.xdg_data / "applications" / install.DESKTOP_NAME
        desktop.write_text(desktop.read_text().replace("MimeType=", f"MimeType={MIME_NEW};{MIME_OLD};", 1))
        p = mock.patch.dict(install.DEFAULT_GROUPS, {"images": [MIME_NEW, MIME_OLD]}, clear=True)
        p.start()
        self.addCleanup(p.stop)
        # MIME_OLD already has a default of someone else's
        (self.xdg_data / "applications" / "other.desktop").write_text(
            f"[Desktop Entry]\nType=Application\nName=Other\nExec={self.binary}\nMimeType=" + MIME_OLD + ";\n")
        (self.xdg_data / "applications" / "user-choice.desktop").write_text(
            f"[Desktop Entry]\nType=Application\nName=Choice\nExec={self.binary}\nMimeType={MIME_OLD};\n")
        (self.xdg_config / "mimeapps.list").write_text("[Default Applications]\n" + MIME_OLD + "=other.desktop\n")

    def test_set_then_restore_puts_every_type_back(self):
        before = self.mimeapps()
        install.set_defaults(["images"])
        self.assertIn(f"{MIME_OLD}={install.DESKTOP_NAME}", self.mimeapps())
        self.assertEqual(install.restore_defaults(), [MIME_OLD])
        after = self.mimeapps()
        self.assertNotIn("Heliograph", after)
        self.assertEqual(after, before, "the file is exactly as it was")

    def test_a_type_that_had_no_default_loses_our_entry_on_restore(self):
        """xdg-mime cannot clear a default, so restore must remove our line itself."""
        real_query = install._query_default
        state = {"taken": False}

        def query(mime):
            if mime == MIME_NEW and not state["taken"]:
                return ""                                  # nobody was the default before we took it
            return real_query(mime)

        with mock.patch.object(install, "_query_default", side_effect=query):
            install.set_defaults(["images"])
            state["taken"] = True
            self.assertIn(f"{MIME_NEW}={install.DESKTOP_NAME}", self.mimeapps())
            self.assertEqual(sorted(install.restore_defaults()), sorted([MIME_NEW, MIME_OLD]))
        after = self.mimeapps()
        self.assertNotIn("Heliograph", after, "no entry of ours may remain for the type that had no default")
        self.assertIn(f"{MIME_OLD}=other.desktop", after)

    def test_running_set_twice_still_restores_the_original(self):
        install.set_defaults(["images"])
        install.set_defaults(["images"])
        install.restore_defaults()
        self.assertIn(f"{MIME_OLD}=other.desktop", self.mimeapps())
        self.assertNotIn("Heliograph", self.mimeapps())

    def test_restore_leaves_a_type_the_user_changed_since(self):
        install.set_defaults(["images"])
        install._remove_default(MIME_OLD)
        (self.xdg_config / "mimeapps.list").write_text(
            self.mimeapps() + f"{MIME_OLD}=user-choice.desktop\n")
        install.restore_defaults()
        self.assertIn(f"{MIME_OLD}=user-choice.desktop", self.mimeapps())


class RemoveDefaultTest(XdgHome):
    def test_removes_only_our_exact_line_in_the_defaults_section(self):
        (self.xdg_config / "mimeapps.list").write_text(
            "[Added Associations]\n" + f"{MIME_NEW}={install.DESKTOP_NAME};\n"
            "[Default Applications]\n" + f"{MIME_NEW}={install.DESKTOP_NAME}\n" + f"{MIME_OLD}={install.DESKTOP_NAME}x\n"
            f"{MIME_OLD}=keep.desktop\n")
        self.assertTrue(install._remove_default(MIME_NEW))
        text = self.mimeapps()
        self.assertIn(f"[Added Associations]\n{MIME_NEW}={install.DESKTOP_NAME};", text)
        self.assertNotIn(f"{MIME_NEW}={install.DESKTOP_NAME}\n", text)
        self.assertIn(f"{MIME_OLD}={install.DESKTOP_NAME}x", text)
        self.assertIn(f"{MIME_OLD}=keep.desktop", text)
        self.assertFalse(install._remove_default(MIME_NEW), "nothing left to remove")
        self.assertFalse(install._remove_default("no/such"), "unknown type")

    def test_missing_file_is_not_an_error(self):
        self.assertFalse(install._remove_default(MIME_NEW))


if __name__ == "__main__":
    unittest.main()
