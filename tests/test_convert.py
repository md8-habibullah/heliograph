import os
import unittest

from helpers import HAVE_FFMPEG, HAVE_PIL, IsolatedHome, tiny_png, wait_for

from heliograph import convert, db
from heliograph.util import ApiError


class ValidationTest(IsolatedHome):
    def test_only_whitelisted_presets_and_options(self):
        convert.validate_spec({"preset": "img-jpg", "quality": "small", "max_size": 800}, "image")
        convert.validate_spec({"preset": "vid-mp4-h264", "max_height": 720, "gpu": "off"}, "video")
        convert.validate_spec({"preset": "aud-mp3"}, "video")            # extracting the sound of a video
        for spec, kind in [({"preset": "rm -rf /"}, "image"), ({"preset": "img-jpg"}, "video"),
                           ({"preset": "img-jpg", "quality": "ultra"}, "image"),
                           ({"preset": "vid-mp4-h264", "max_height": 999}, "video"),
                           ({"preset": "img-jpg", "max_size": 10**6}, "image"),
                           ({"preset": "img-jpg", "max_size": "abc"}, "image"),
                           ({"preset": "vid-mp4-h264", "gpu": "yes"}, "video"),
                           ({"preset": "vid-mkv-copy", "max_height": 720}, "video"),
                           ({"preset": "img-jpg", "out_dir": "/etc"}, "image"),
                           ({"preset": "img-jpg", "out_dir": "/does/not/exist"}, "image")]:
            with self.assertRaises(ApiError, msg=str(spec)):
                convert.validate_spec(spec, kind)

    def test_every_preset_has_consistent_metadata(self):
        for pid, (label, desc, kinds, okind, ext, muxer) in convert.PRESETS.items():
            self.assertTrue(label and desc and ext and muxer, pid)
            self.assertTrue(set(kinds) <= {"image", "video", "audio"}, pid)
            self.assertIn(okind, {"image", "video", "audio"}, pid)
        self.assertGreaterEqual(len(convert.PRESETS), 20)

    def test_unique_path_never_overwrites(self):
        a = self.touch("pic.jpg")
        self.touch("pic (1).jpg")
        p = convert.unique_path(str(a.parent), "pic", "jpg")
        self.assertEqual(os.path.basename(p), "pic (2).jpg")
        self.assertFalse(os.path.exists(p))

    @unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
    def test_commands_are_argument_lists_with_absolute_sources(self):
        spec = {"preset": "img-webp", "quality": "balanced", "max_size": 640, "strip": True}
        cmds = convert._image_cmds(spec, "/pics/-weird name.heic", "/out/x.webp", {})
        for cmd in cmds:
            self.assertTrue(all(isinstance(x, str) for x in cmd))
            self.assertIn("/pics/-weird name.heic", " ".join(cmd))
        ff = [c for c in cmds if os.path.basename(c[0]).startswith("ffmpeg")][0]
        self.assertEqual(ff[ff.index("-i") + 1], "/pics/-weird name.heic")


@unittest.skipUnless(HAVE_FFMPEG and HAVE_PIL, "needs ffmpeg and Pillow")
class RunTest(IsolatedHome):
    def test_picture_conversion_end_to_end(self):
        src = tiny_png(str(self.lib / "red.png"), (400, 300))
        ix, _ = self.index()
        row = db.conn().execute("SELECT * FROM files WHERE name='red.png'").fetchone()
        out = self.lib / "out"
        out.mkdir()
        jm = convert.JobManager()
        jm.start()
        self.addCleanup(jm.stop)
        ids = jm.submit([row], {"preset": "img-jpg", "quality": "high", "max_size": 100, "out_dir": str(out)})
        self.assertTrue(wait_for(lambda: db.conn().execute("SELECT status FROM jobs WHERE id=?", (ids[0],)).fetchone()[0] in ("done", "failed"), 60))
        job = db.conn().execute("SELECT * FROM jobs WHERE id=?", (ids[0],)).fetchone()
        self.assertEqual(job["status"], "done", job["message"])
        from PIL import Image
        with Image.open(job["dst"]) as im:
            self.assertEqual(im.format, "JPEG")
            self.assertLessEqual(max(im.size), 100)
        self.assertTrue(os.path.exists(src), "the original is untouched")
        self.assertEqual(os.listdir(out), ["red.jpg"], "no .part file is left behind")
        mode = os.stat(job["dst"]).st_mode & 0o777
        self.assertEqual(mode & 0o600, 0o600)


if __name__ == "__main__":
    unittest.main()


# Presets that need an optional encoder (not part of every FFmpeg build, e.g. the one a CI image ships).
# Every other preset must work with any normal FFmpeg and always fails the test if it does not.
ENCODER_DEPENDENT = {"img-avif", "img-jxl", "vid-webm-av1", "vid-mp4-hevc", "vid-mov-prores", "vid-webp"}


@unittest.skipUnless(HAVE_FFMPEG and HAVE_PIL, "needs ffmpeg and Pillow")
class EveryPresetTest(IsolatedHome):
    """Every preset must produce a readable file of the right kind from a tiny real input. A preset whose encoder
    is not compiled into this FFmpeg is skipped (reported), never silently passed."""

    @classmethod
    def setUpClass(cls):
        import subprocess
        import tempfile
        cls._dir = tempfile.TemporaryDirectory(prefix="heliograph-src-")
        d = cls._dir.name
        tiny_png(f"{d}/pic.png", (160, 120))
        base = ["ffmpeg", "-v", "error", "-y"]
        subprocess.run(base + ["-f", "lavfi", "-i", "testsrc=size=160x120:rate=10", "-f", "lavfi", "-i", "sine=frequency=330",
                               "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", f"{d}/clip.mp4"], check=True)
        subprocess.run(base + ["-f", "lavfi", "-i", "sine=frequency=440", "-t", "1", f"{d}/tone.wav"], check=True)
        cls.sources = {"image": f"{d}/pic.png", "video": f"{d}/clip.mp4", "audio": f"{d}/tone.wav"}

    @classmethod
    def tearDownClass(cls):
        cls._dir.cleanup()

    def test_all_presets(self):
        import json
        import subprocess
        skipped, failed = [], []
        for pid, (label, desc, kinds, okind, ext, muxer) in convert.PRESETS.items():
            kind = "image" if "image" in kinds else "video" if "video" in kinds else "audio"
            out = self.lib / pid
            out.mkdir()
            resizable = okind == "video" and not pid.endswith("-copy")       # a stream copy cannot resize
            spec = {"src": self.sources[kind], "kind": kind, "preset": pid, "quality": "small", "gpu": "off",
                    "out_dir": str(out), "max_height": 96 if resizable else 0, "max_size": 96 if okind == "image" else 0}
            spec = {k: v for k, v in spec.items() if v != 0}
            try:
                res = convert.run_spec(spec)
            except RuntimeError as e:
                msg = str(e)
                if pid in ENCODER_DEPENDENT:
                    skipped.append(f"{pid}: {msg[:80]}")         # this FFmpeg build may simply lack the encoder
                else:
                    failed.append(f"{pid}: {msg[:200]}")
                continue
            files = sorted(p.name for p in out.iterdir())
            self.assertEqual(files, [f"{os.path.splitext(os.path.basename(spec['src']))[0]}.{ext}"], f"{pid}: only the result, no .part")
            self.assertGreater(res["bytes_out"], 0, pid)
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", res["dst"]],
                                   capture_output=True, text=True)
            self.assertEqual(probe.returncode, 0, f"{pid}: the result cannot be read back: {probe.stderr[:150]}")
            streams = json.loads(probe.stdout)["streams"]
            types = {s["codec_type"] for s in streams}
            if okind == "audio":
                self.assertEqual(types, {"audio"}, pid)
            elif okind == "video":
                self.assertIn("video", types, pid)
                if resizable:
                    self.assertLessEqual(min(s["height"] for s in streams if s["codec_type"] == "video"), 96, f"{pid}: height limit")
            else:
                self.assertIn("video", types, pid)                   # FFmpeg reports a still picture as a video stream
                self.assertLessEqual(max(max(s["width"], s["height"]) for s in streams if s["codec_type"] == "video"), 96, pid)
        if skipped:
            print("\n  skipped (encoder missing in this FFmpeg):", *skipped, sep="\n    ")
        self.assertEqual(failed, [], "presets that failed:\n" + "\n".join(failed))
