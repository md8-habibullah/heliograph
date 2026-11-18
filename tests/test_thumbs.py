"""Thumbnail generation with the Pillow versions that distributions actually ship."""

import io
import types
import unittest

from helpers import HAVE_PIL, IsolatedHome, tiny_png

from heliograph import thumbs


class LanczosTest(unittest.TestCase):
    def test_finds_the_filter_on_old_and_new_pillow(self):
        """Pillow before 9.1 (Ubuntu 22.04 ships 9.0.1) has no Image.Resampling. Using it unconditionally made every
        Pillow thumbnail fail quietly and fall back to the much slower ImageMagick and FFmpeg paths."""
        old = types.SimpleNamespace(LANCZOS="old-constant")
        new = types.SimpleNamespace(Resampling=types.SimpleNamespace(LANCZOS="new-constant"), LANCZOS="alias")
        self.assertEqual(thumbs._lanczos(old), "old-constant")
        self.assertEqual(thumbs._lanczos(new), "new-constant")


@unittest.skipUnless(HAVE_PIL, "needs Pillow")
class PillowThumbnailTest(IsolatedHome):
    def test_fast_path_with_the_installed_pillow(self):
        from PIL import Image
        for size, expected in (((300, 600), (50, 100)), ((400, 300), (100, 75))):
            data = thumbs._pil_make(tiny_png(str(self.lib / f"{size[0]}.png"), size), 100, 82)
            self.assertTrue(data.startswith(b"\xff\xd8"), "a JPEG came out")
            with Image.open(io.BytesIO(data)) as im:
                self.assertEqual(im.size, expected)


if __name__ == "__main__":
    unittest.main()
