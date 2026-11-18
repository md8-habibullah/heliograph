"""Static checks of the user interface files: no unsafe DOM APIs, every element the scripts look up exists."""

import re
import shutil
import subprocess
import unittest

from helpers import SRC

WEB = SRC / "heliograph" / "web"
JS = sorted(WEB.glob("*.js"))


class WebAssetsTest(unittest.TestCase):
    def test_files_exist(self):
        for name in ("index.html", "style.css", "icon.svg", "main.js", "core.js", "grid.js", "nav.js", "player.js", "dialogs.js", "help.js"):
            self.assertTrue((WEB / name).is_file(), name)

    def test_no_unsafe_dom_apis(self):
        bad = re.compile(r"\.(innerHTML|outerHTML)\s*=|insertAdjacentHTML|document\.write\(|\beval\(|new Function\(|\.srcdoc\s*=")
        for f in JS:
            for n, line in enumerate(f.read_text().splitlines(), 1):
                self.assertIsNone(bad.search(line), f"{f.name}:{n}: {line.strip()}")

    def test_no_external_requests(self):
        for f in JS + [WEB / "index.html", WEB / "style.css"]:
            text = f.read_text()
            for m in re.finditer(r"""(?:src|href)=["'](https?:)?//|url\(\s*["']?https?:|fetch\(\s*["']https?:|import\s+.*from\s+["']https?:""", text):
                self.fail(f"{f.name} references an external resource: {m.group(0)}")

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_nothing_is_cached_or_requested_by_file_id_alone(self):
        """File ids are handed to other files after a folder is removed. A cache or a (one-year cached) URL that uses the
        id alone shows the previous folder's pictures on the new folder's tiles. Everything must also carry the file's tag."""
        for f in JS:
            text = f.read_text()
            for m in re.finditer(r"`/(?:thumb|media|preview)/\$\{[^}]+\}`", text):
                self.fail(f"{f.name}: URL built from an id without the tag: {m.group(0)} (use fileUrl() from core.js)")
            for m in re.finditer(r"loader\.(?:url|failedFor|settled|request)\(\s*[^,()]+\)", text):
                self.fail(f"{f.name}: thumbnail cache looked up by id alone: {m.group(0)}")
        grid = (WEB / "grid.js").read_text()
        self.assertIn("const tkey = (id, k) =>", grid)
        self.assertNotIn("cache.has(el._id)", grid)

    def test_help_and_about_are_wired_up(self):
        html = (WEB / "index.html").read_text()
        for ident in ("help", "h-tabs", "h-body", "h-close", "v-help", "btn-help", "btn-about", "i-help"):
            self.assertIn(f'id="{ident}"', html, ident)
        main, player = (WEB / "main.js").read_text(), (WEB / "player.js").read_text()
        self.assertIn("createHelp", main)
        self.assertIn("case '?'", main, "? opens help in the library")
        self.assertIn("case '?'", player, "? opens help in the viewer")
        self.assertIn("onHelp", player)

    def test_about_links_go_only_to_the_developer_and_the_repository(self):
        """The only addresses in the page are the two the About tab shows (they are links, never requests)."""
        from heliograph import AUTHOR_SITE, REPO_URL
        found = set()
        for f in JS:
            found |= set(re.findall(r"https?://[A-Za-z0-9./_-]+", f.read_text()))
        found = {u for u in found if "w3.org" not in u}
        self.assertEqual(found, {AUTHOR_SITE, REPO_URL}, "any other address in the interface needs a deliberate decision")
        text = (WEB / "help.js").read_text()
        self.assertIn("target: '_blank'", text)
        self.assertIn("noopener noreferrer", text)

    def test_help_text_matches_the_key_handlers(self):
        """Every letter key the help table promises is handled somewhere. (A new key needs a row; a row needs a handler.)"""
        help_js = (WEB / "help.js").read_text()
        handlers = (WEB / "player.js").read_text() + (WEB / "main.js").read_text()
        letters = set(re.findall(r"'([A-Z])'", help_js))
        self.assertTrue({"K", "J", "L", "M", "R", "C", "F", "I", "S", "Z", "X"} <= letters, letters)
        for k in letters:
            self.assertRegex(handlers, r"case '" + k.lower() + r"'", f"help lists {k} but nothing handles it")
        self.assertRegex(handlers, r"'n': .*Shift\+N", "Shift+N is next track")

    def test_playback_speeds_go_up_to_three_and_a_half(self):
        m = re.search(r"const SPEEDS = \[([^\]]+)\]", (WEB / "player.js").read_text())
        speeds = [float(x) for x in m.group(1).split(",")]
        self.assertEqual((speeds[0], speeds[-1]), (0.25, 3.5))
        self.assertIn(1, speeds)
        self.assertEqual(speeds, sorted(speeds))

    @unittest.skipUnless(shutil.which("node"), "needs node (a syntax check only; CI runs it)")
    def test_scripts_parse(self):
        for f in JS:
            r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{f.name}: {r.stderr}")

    def test_elements_looked_up_by_id_exist(self):
        html = (WEB / "index.html").read_text()
        ids = set(re.findall(r'\bid="([\w-]+)"', html))
        dynamic = set()
        for f in JS:
            dynamic |= set(re.findall(r"""\bid:\s*['"]([\w-]+)['"]""", f.read_text()))
        for f in JS:
            text = f.read_text()
            for m in re.finditer(r"""\$\(\s*['"]#([\w-]+)['"]|getElementById\(\s*['"]([\w-]+)['"]""", text):
                i = m.group(1) or m.group(2)
                self.assertIn(i, ids | dynamic, f"{f.name} looks for #{i}, which is not in index.html")

    def test_icons_used_exist(self):
        html = (WEB / "index.html").read_text()
        symbols = set(re.findall(r'<symbol id="i-([\w-]+)"', html))
        for f in JS:
            for m in re.finditer(r"""\bicon\(\s*['"]([\w-]+)['"]""", f.read_text()):
                self.assertIn(m.group(1), symbols, f"{f.name} uses missing icon {m.group(1)}")

    def test_page_placeholders(self):
        html = (WEB / "index.html").read_text()
        self.assertIn("__TOKEN__", html)
        self.assertIn("__V__", html)
        self.assertIn('<meta name="viewport"', html)
        self.assertIn('lang="en"', html)


if __name__ == "__main__":
    unittest.main()
