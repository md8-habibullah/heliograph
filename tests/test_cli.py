"""The `heliograph` command line, run as a real subprocess against an isolated home."""

import json
import os
import subprocess
import sys
import unittest

from helpers import HAVE_FFMPEG, ROOT, IsolatedHome


class CliTest(IsolatedHome):
    def run_cli(self, *args, check=True):
        env = dict(os.environ, HELIOGRAPH_HOME=str(self.home), PYTHONPATH=str(ROOT / "src"))
        r = subprocess.run([sys.executable, "-m", "heliograph", *args], capture_output=True, text=True, env=env, timeout=60)
        if check:
            self.assertEqual(r.returncode, 0, f"{args}: {r.stderr}")
        return r

    def test_version(self):
        from heliograph import __version__
        self.assertIn(f"Heliograph {__version__}", self.run_cli("--version").stdout)

    def test_config_roundtrip_and_validation(self):
        out = self.run_cli("config").stdout
        for key in ("bg_index", "thumb_cache_mb", "transcode_gpu", "transcode_max_height", "idle_exit_min"):
            self.assertIn(key, out)
        self.assertEqual(self.run_cli("config", "thumb_cache_mb").stdout.strip(), "2048")
        self.run_cli("config", "thumb_cache_mb", "512")
        self.assertEqual(self.run_cli("config", "thumb_cache_mb").stdout.strip(), "512")
        self.assertEqual(json.loads(self.run_cli("config", "--json").stdout)["thumb_cache_mb"], "512")
        snap = json.loads((self.home / "data" / "user-data.json").read_text())
        self.assertEqual(snap["settings"]["thumb_cache_mb"], "512", "settings are in the recovery snapshot at once")
        bad = self.run_cli("config", "bg_index", "sometimes", check=False)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("must be one of", bad.stderr)
        self.assertEqual(self.run_cli("config", "no_such_key", check=False).returncode, 1)
        self.assertEqual(self.run_cli("config", "thumb_cache_mb").stdout.strip(), "512", "a rejected value changes nothing")

    def test_search_reads_the_library_without_a_service(self):
        self.touch("trip/holiday one.jpg")
        self.touch("trip/holiday two.jpg")
        self.touch("trip/clip.mp4")
        self.index()
        out = self.run_cli("search", "holiday", "--json").stdout
        rows = [json.loads(line) for line in out.splitlines() if line.strip()]     # one JSON object per line
        self.assertEqual(len(rows), 2)
        self.assertTrue(all("holiday" in r["path"] for r in rows))
        out = self.run_cli("search", "--kind", "video", "--json").stdout
        self.assertIn("clip.mp4", out)
        self.assertNotIn("holiday", out)

    def test_convert_list_and_formats(self):
        listing = self.run_cli("convert", "--list").stdout
        for pid in ("img-jpg", "vid-mp4-h264", "aud-flac"):
            self.assertIn(pid, listing)
        data = json.loads(self.run_cli("formats", "--json").stdout)
        self.assertEqual(data["indexed_extensions"], {"image": len(data["image"]), "video": len(data["video"]), "audio": len(data["audio"])})
        self.assertGreaterEqual(len(data["image"]) + len(data["video"]) + len(data["audio"]), 240)
        self.assertIn("mov", data["video"])
        self.assertIn("ffmpeg", data["tools"])

    def test_unknown_files_are_refused_politely(self):
        r = self.run_cli("info", str(self.touch("notes.txt")), check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a picture, video or audio file", r.stderr)
        r = self.run_cli("convert", "--to", "mp4", "/no/such/file.mov", check=False)
        self.assertNotEqual(r.returncode, 0)

    @unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
    def test_info_json(self):
        p = self.lib / "t.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=10", "-f", "lavfi", "-i",
                        "sine", "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(p)], check=True)
        d = json.loads(self.run_cli("info", str(p), "--json").stdout)
        self.assertEqual(d["kind"], "video")
        self.assertEqual(d["info"]["video"]["codec"], "h264")
        self.assertEqual(d["play"]["mode"], "direct")


if __name__ == "__main__":
    unittest.main()
