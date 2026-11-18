import unittest

from helpers import IsolatedHome  # noqa: F401  (sets up sys.path)

from heliograph import formats


class FormatsTest(unittest.TestCase):
    def test_breadth(self):
        n = len(formats.KIND_OF)
        self.assertGreaterEqual(n, 240, "the app promises hundreds of picture, video and audio extensions")
        kinds = set(formats.KIND_OF.values())
        self.assertEqual(kinds, {"image", "video", "audio"})

    def test_common_formats(self):
        for name, kind in [("a.JPG", "image"), ("a.heic", "image"), ("a.CR2", "image"), ("a.psd", "image"), ("a.svg", "image"),
                           ("a.mov", "video"), ("a.mkv", "video"), ("a.wmv", "video"), ("a.mpg", "video"), ("a.webm", "video"),
                           ("a.flac", "audio"), ("a.m4a", "audio"), ("a.opus", "audio"), ("a.wma", "audio")]:
            self.assertEqual(formats.kind_of(name), kind, name)

    def test_unknown_and_ambiguous(self):
        for name in ("notes.txt", "archive.zip", "README", ".jpg", "data.json", "x.ps", "x.rec"):
            self.assertIsNone(formats.kind_of(name), name)

    def test_ext_of(self):
        self.assertEqual(formats.ext_of("holiday.TAR.GZ"), "gz")
        self.assertEqual(formats.ext_of("noext"), "")
        self.assertEqual(formats.ext_of(".hidden"), "")

    def test_mime(self):
        self.assertEqual(formats.mime_of("a.mkv"), "video/x-matroska")
        self.assertTrue(formats.mime_of("a.jpg").startswith("image/"))
        self.assertTrue(formats.mime_of("a.unknownext"))

    def test_desktop_mime_list(self):
        mimes = formats.mime_types_for_desktop_file()
        self.assertIn("video/mp4", mimes)
        self.assertEqual(len(mimes), len(set(mimes)))


if __name__ == "__main__":
    unittest.main()
