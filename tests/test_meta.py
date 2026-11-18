import os
import subprocess
import unittest

from helpers import HAVE_FFMPEG, HAVE_PIL, IsolatedHome, tiny_png

from heliograph import meta


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class BmffTest(IsolatedHome):
    def make(self, name, *extra):
        p = str(self.lib / name)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=10", "-f", "lavfi",
                        "-i", "sine=frequency=440", "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        "-metadata", "title=Hello", "-metadata", "artist=Me", *extra, p], check=True)
        return p

    def test_header_parser_matches_ffprobe_without_starting_it(self):
        p = self.make("a.mp4")
        m = meta.probe_bmff(p)
        self.assertEqual((m["w"], m["h"]), (64, 48))
        self.assertAlmostEqual(m["dur"], 2.0, delta=0.1)
        self.assertEqual(m["codec"], "h264")
        self.assertEqual((m["title"], m["artist"]), ("Hello", "Me"))
        j = meta.ffprobe_json(p)
        self.assertAlmostEqual(meta.summarize(j)["dur"], m["dur"], delta=0.1)

    def test_mov_and_faststart_layouts(self):
        for name, extra in (("a.mov", []), ("b.mp4", ["-movflags", "+faststart"]), ("c.m4v", [])):
            m = meta.probe_bmff(self.make(name, *extra))
            self.assertEqual((m["w"], m["h"]), (64, 48), name)

    def test_garbage_is_not_an_error(self):
        p = self.lib / "bad.mp4"
        p.write_bytes(os.urandom(5000))
        self.assertFalse(meta.probe_bmff(str(p)))
        self.assertFalse(meta.probe_bmff(str(self.lib / "missing.mp4")))
        (self.lib / "empty.mov").write_bytes(b"")
        self.assertFalse(meta.probe_bmff(str(self.lib / "empty.mov")))


@unittest.skipUnless(HAVE_PIL, "needs Pillow")
class ImageTest(IsolatedHome):
    def test_dimensions_without_decoding(self):
        p = tiny_png(str(self.lib / "a.png"), (300, 200))
        self.assertEqual({k: meta.probe_image(p)[k] for k in ("w", "h")}, {"w": 300, "h": 200})

    def test_exif_orientation_swaps_dimensions(self):
        from PIL import Image
        im = Image.new("RGB", (300, 200))
        exif = Image.Exif()
        exif[0x0112] = 6                                   # rotated 90 degrees
        p = str(self.lib / "r.jpg")
        im.save(p, exif=exif)
        m = meta.probe_image(p)
        self.assertEqual((m["w"], m["h"]), (200, 300))

    def test_broken_picture_is_not_an_error(self):
        p = self.lib / "x.jpg"
        p.write_bytes(b"not a jpeg")
        self.assertEqual(meta.probe_cheap("image", "jpg", str(p)), {}, "unreadable means 'no details', never a crash")
        (self.lib / "empty.png").write_bytes(b"")
        self.assertEqual(meta.probe_cheap("image", "png", str(self.lib / "empty.png")), {})


if __name__ == "__main__":
    unittest.main()
