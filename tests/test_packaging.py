"""Packaging files stay in step with the code, and are valid."""

import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from helpers import ROOT, SRC, IsolatedHome  # noqa: F401  (sets up sys.path)

from heliograph import APP_ID, __version__, install

PKG = ROOT / "packaging"
sys.path.insert(0, str(PKG))
import build_packages as bp  # noqa: E402


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
        self.assertEqual(re.search(r"^pkgver=(\S+)", (PKG / "arch" / "PKGBUILD").read_text(), re.M).group(1), __version__)
        spec = (PKG / "rpm" / "heliograph.spec").read_text()
        self.assertEqual(re.search(r"^Version:\s+(\S+)", spec, re.M).group(1), __version__)
        self.assertRegex(spec, rf"(?m)^\* .+ - {re.escape(__version__)}-1$", "the newest rpm changelog entry names this version")
        self.assertEqual(bp.version(), __version__)
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


FIXED_FILES = {
    "usr/bin/heliograph",
    f"usr/share/applications/{APP_ID}.desktop",
    f"usr/share/icons/hicolor/scalable/apps/{APP_ID}.svg",
    f"usr/share/metainfo/{APP_ID}.metainfo.xml",
    "usr/share/man/man1/heliograph.1.gz",
}


class StageTest(unittest.TestCase):
    """The one step that decides what every package (.deb, .rpm, Arch) installs."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="heliograph-stage-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "root"

    def test_installs_the_program_the_launcher_and_the_desktop_files(self):
        made = set(bp.stage(self.root, "/usr", "none"))
        python_files = {f"usr/share/heliograph/heliograph/{p.relative_to(SRC / 'heliograph')}" for p in bp.package_files()}
        self.assertEqual(made, python_files | FIXED_FILES)
        self.assertIn("usr/share/heliograph/heliograph/web/index.html", made, "the web interface is part of the package")
        self.assertFalse([m for m in made if "__pycache__" in m or m.endswith(".pyc")], "no compiled caches are shipped")
        for rel in made:
            self.assertTrue((self.root / rel).is_file(), rel)

    def test_permissions_do_not_depend_on_the_builders_umask(self):
        old = os.umask(0o077)
        try:
            made = bp.stage(self.root, "/usr", "none")
        finally:
            os.umask(old)
        for rel in made:
            mode = (self.root / rel).stat().st_mode & 0o777
            self.assertEqual(mode, 0o755 if rel == "usr/bin/heliograph" else 0o644, rel)
        for d in [self.root, *[p for p in self.root.rglob("*") if p.is_dir()]]:
            self.assertEqual(d.stat().st_mode & 0o777, 0o755, str(d))

    def test_licence_and_docs_layout_per_package_type(self):
        self.assertFalse([m for m in bp.stage(self.root / "a", "/usr", "none") if "licenses" in m or "/doc/" in m])
        deb = set(bp.stage(self.root / "b", "/usr", "deb"))
        self.assertIn("usr/share/doc/heliograph/copyright", deb)
        self.assertIn("usr/share/licenses/heliograph/LICENSE", set(bp.stage(self.root / "c", "/usr", "arch")))
        copyright_text = (self.root / "b" / "usr/share/doc/heliograph/copyright").read_text()
        self.assertTrue(copyright_text.startswith("Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/"))
        self.assertIn("License: MIT", copyright_text)
        self.assertIn("Copyright: 2026 MD. Habibullah Sharif", copyright_text)
        self.assertIn("Permission is hereby granted", copyright_text)

    def test_prefix_is_respected(self):
        made = bp.stage(self.root, "/usr/local", "none")
        self.assertIn("usr/local/bin/heliograph", made)
        self.assertIn("usr/local/share/heliograph/heliograph/cli.py", made)

    def test_the_desktop_file_starts_a_program_that_is_installed(self):
        made = set(bp.stage(self.root, "/usr", "none"))
        exec_line = next(ln for ln in (self.root / f"usr/share/applications/{APP_ID}.desktop").read_text().splitlines() if ln.startswith("Exec="))
        self.assertIn(f"usr/bin/{exec_line[5:].split()[0]}", made)

    def test_launcher_runs_from_any_folder_and_finds_its_files(self):
        bp.stage(self.root, "/usr", "none")
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        env["HELIOGRAPH_HOME"] = str(self.root.parent / "home")
        # -I: ignore PYTHONPATH and user site-packages, so a copy installed elsewhere cannot make this pass by accident.
        r = subprocess.run([sys.executable, "-I", str(self.root / "usr/bin/heliograph"), "--version"], cwd="/",
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, f"Heliograph {__version__}"), r.stderr)
        self.assertEqual(list(self.root.rglob("__pycache__")), [], "running the program must not write caches into the install folder")

    def test_launcher_names_the_python_it_needs(self):
        self.assertTrue(bp.LAUNCHER.startswith("#!/usr/bin/python3\n"))
        self.assertIn("(3, 10)", bp.LAUNCHER)


class StagedServiceTest(IsolatedHome):
    """The installed copy must be able to start its own background service (it is a separate `python -m` process)."""

    def test_service_starts_from_the_installed_layout_and_stops(self):
        root = self.home.parent / "installed"
        bp.stage(root, "/usr", "none")
        (self.lib / "a.jpg").write_bytes(b"\xff\xd8\xff\xd9")
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        env["HELIOGRAPH_HOME"] = str(self.home)
        launcher = [sys.executable, "-I", str(root / "usr/bin/heliograph")]
        try:
            r = subprocess.run([*launcher, "--no-window", str(self.lib)], capture_output=True, text=True, env=env, timeout=40, cwd="/")
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
            self.assertIn("service running on port", r.stdout)
            st = subprocess.run([*launcher, "status", "--json"], capture_output=True, text=True, env=env, timeout=20, cwd="/")
            self.assertEqual(st.returncode, 0, st.stderr)
        finally:
            subprocess.run([*launcher, "stop"], capture_output=True, text=True, env=env, timeout=20, cwd="/")
        self.assertEqual(list(root.rglob("__pycache__")), [], "neither the command nor the background service writes caches there")


class DebTest(unittest.TestCase):
    def test_control_file(self):
        text = bp.deb_control("1.2.3", 42)
        fields = dict(re.findall(r"^([A-Za-z-]+): (.*)$", text, re.M))
        self.assertEqual(fields["Package"], "heliograph")
        self.assertEqual(fields["Version"], "1.2.3")
        self.assertEqual(fields["Architecture"], "all")
        self.assertEqual(fields["Installed-Size"], "42")
        self.assertEqual(fields["Depends"], "python3 (>= 3.10), hicolor-icon-theme",
                         "only Python and the icon theme folders are required; FFmpeg and a browser are recommended")
        self.assertIn("ffmpeg", fields["Recommends"])
        self.assertIn("chromium", fields["Recommends"])
        self.assertRegex(fields["Maintainer"], r"^MD\. Habibullah Sharif <[^<>@\s]+@users\.noreply\.github\.com>$")
        description = text.split("Description:", 1)[1].splitlines()[1:]
        self.assertTrue(description and all(ln.startswith(" ") for ln in description if ln != ""), "continuation lines start with a space")
        self.assertTrue(text.endswith("\n"))

    @unittest.skipUnless(shutil.which("dpkg-deb"), "needs dpkg-deb")
    def test_builds_a_valid_deb(self):
        with tempfile.TemporaryDirectory(prefix="heliograph-deb-test-") as out:
            (deb,) = bp.build_deb(Path(out))
            self.assertEqual(deb.name, f"heliograph_{__version__}_all.deb")
            info = subprocess.run(["dpkg-deb", "--field", str(deb)], capture_output=True, text=True).stdout
            self.assertIn(f"Version: {__version__}", info)
            listing = subprocess.run(["dpkg-deb", "--contents", str(deb)], capture_output=True, text=True).stdout
            self.assertIn("./usr/bin/heliograph", listing)
            self.assertIn("./usr/share/doc/heliograph/copyright", listing)
            self.assertNotIn("__pycache__", listing)
            owners = {ln.split()[1] for ln in listing.splitlines()}
            self.assertEqual(owners, {"root/root"}, "files belong to root, not to whoever built the package")


class SpecAndPkgbuildTest(unittest.TestCase):
    def spec(self):
        return (PKG / "rpm" / "heliograph.spec").read_text()

    def test_spec_lists_exactly_what_stage_installs(self):
        with tempfile.TemporaryDirectory(prefix="heliograph-spec-test-") as tmp:
            staged = set(bp.stage(Path(tmp), "/usr", "none"))
        section = self.spec().split("\n%files", 1)[1].split("\n%changelog", 1)[0]
        entries = []
        for line in section.splitlines()[1:]:
            line = line.strip()
            if not line or line.startswith(("%license", "%doc")):
                continue
            for macro, value in (("%{_bindir}", "/usr/bin"), ("%{_mandir}", "/usr/share/man"), ("%{_datadir}", "/usr/share"), ("%{_prefix}", "/usr")):
                line = line.replace(macro, value)
            self.assertNotIn("%", line, f"unexpanded macro in the test: {line}")
            entries.append(line.lstrip("/"))
        for rel in staged:
            self.assertTrue(any(fnmatch.fnmatchcase(rel, e) or (e.endswith("/") and rel.startswith(e)) for e in entries), f"{rel} is installed but not in %files")
        for e in entries:
            self.assertTrue(any(fnmatch.fnmatchcase(rel, e) or (e.endswith("/") and rel.startswith(e)) for rel in staged), f"%files lists {e}, which is not installed")

    def test_spec_basics(self):
        spec = self.spec()
        self.assertIn("BuildArch:      noarch", spec)
        self.assertIn("Requires:       python3 >= 3.10", spec)
        self.assertIn("Requires:       hicolor-icon-theme", spec, "owns the icon folders the package installs into")
        self.assertIn("--docs none", spec, "the rpm adds its licence and docs with %license and %doc")
        for name in ("LICENSE", "README.md", "CHANGELOG.md"):
            self.assertTrue((ROOT / name).is_file())
            self.assertIn(name, spec)

    def test_pkgbuild_uses_the_shared_staging_step(self):
        text = (PKG / "arch" / "PKGBUILD").read_text()
        self.assertIn('python packaging/build_packages.py stage --root "$pkgdir" --prefix /usr --docs arch', text)
        # build_arch() swaps these two lines for a local source; each must exist once, on a single line.
        self.assertEqual(len(re.findall(r"^source=\(.*\)$", text, re.M)), 1)
        self.assertEqual(len(re.findall(r"^sha256sums=\(.*\)", text, re.M)), 1)
        self.assertIn("arch=('any')", text)
        self.assertRegex(text, r"(?m)^depends=\(.*'hicolor-icon-theme'", "namcap: icons in hicolor need the icon theme")


class ManPageTest(unittest.TestCase):
    def setUp(self):
        self.text = (PKG / "heliograph.1").read_text()

    def test_every_command_is_documented(self):
        from heliograph import cli
        for name in cli.COMMANDS:
            self.assertIn(f".TP\n.B {name}\n", self.text, f"`heliograph {name}` is missing from packaging/heliograph.1")

    def test_environment_variables_and_options_are_documented(self):
        for needle in ("HELIOGRAPH_HOME", "HELIOGRAPH_BROWSER", "\\-\\-add", "\\-\\-tab", "\\-\\-no\\-window", "\\-\\-version"):
            self.assertIn(needle, self.text)
        self.assertNotRegex(self.text, r"(?m)^\.(TH|SH) .*0\.\d+\.\d+", "no version number in the page: it would go stale")

    @unittest.skipUnless(shutil.which("groff"), "needs groff")
    def test_page_has_no_roff_warnings(self):
        r = subprocess.run(["groff", "-man", "-ww", "-z", str(PKG / "heliograph.1")], capture_output=True, text=True)
        self.assertEqual(r.stderr.strip(), "", r.stderr)


class SourceArchiveTest(unittest.TestCase):
    def build(self, directory, stamp):
        old = os.environ.get("SOURCE_DATE_EPOCH")
        os.environ["SOURCE_DATE_EPOCH"] = stamp
        try:
            return bp.source_tarball(Path(directory) / "src.tar.gz", "9.9.9")
        finally:
            if old is None:
                del os.environ["SOURCE_DATE_EPOCH"]
            else:
                os.environ["SOURCE_DATE_EPOCH"] = old

    def test_layout_is_like_githubs_archive_of_a_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.build(tmp, "1700000000")
            with tarfile.open(path) as tar:
                names = tar.getnames()
                self.assertTrue(all(n.startswith("heliograph-9.9.9/") for n in names))
                for needed in ("packaging/build_packages.py", "packaging/arch/PKGBUILD", "packaging/rpm/heliograph.spec",
                               "src/heliograph/__init__.py", "LICENSE", "README.md", "CHANGELOG.md"):
                    self.assertIn(f"heliograph-9.9.9/{needed}", names)
                self.assertFalse([n for n in names if "/.git/" in n or "__pycache__" in n])
                self.assertTrue(all(m.uid == 0 and m.gid == 0 and m.mtime == 1700000000 for m in tar.getmembers()))

    def test_same_input_gives_the_same_bytes(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first = hashlib.sha256(self.build(a, "1700000000").read_bytes()).hexdigest()
            second = hashlib.sha256(self.build(b, "1700000000").read_bytes()).hexdigest()
        self.assertEqual(first, second)

    def test_checksum_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "b.txt").write_bytes(b"two")
            (out / "a.txt").write_bytes(b"one")
            (out / "SHA256SUMS").write_text("stale\n")
            bp.write_sums(out)
            lines = (out / "SHA256SUMS").read_text().splitlines()
            self.assertEqual(lines, [f"{hashlib.sha256(b'one').hexdigest()}  a.txt", f"{hashlib.sha256(b'two').hexdigest()}  b.txt"])


class ReleaseWorkflowTest(unittest.TestCase):
    def test_workflow_builds_every_package_type_and_pins_its_actions(self):
        text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
        for step in ("build_packages.py deb", "build_packages.py rpm", "build_packages.py arch", "build_packages.py sums"):
            self.assertIn(step, text)
        uses = re.findall(r"^\s*-?\s*uses:\s*(\S+)", text, re.M)
        self.assertTrue(uses)
        for ref in uses:
            self.assertRegex(ref, r"@(v\d+(\.\d+)*|release/v\d+)$", f"{ref}: pin a version, never a moving branch")
        self.assertEqual(text.count("contents: write"), 1, "only the job that publishes the release may write")


if __name__ == "__main__":
    unittest.main()
