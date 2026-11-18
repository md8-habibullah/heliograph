import json
import subprocess
import unittest

from helpers import HAVE_FFMPEG, IsolatedHome

from heliograph import playback


def video(codec="h264", **kw):
    v = {"index": 0, "codec": codec, "profile": "high", "pix_fmt": "yuv420p", "w": 1920, "h": 1080, "fps": 25,
         "rotation": 0, "transfer": "bt709", "field_order": "progressive", "bits": 8}
    v.update(kw)
    return v


def info(container="mov,mp4,m4a,3gp,3g2,mj2", v=None, audio=("aac",), **kw):
    d = {"container": container, "video": v if v is not False else None, "dur": 10.0,
         "audio": [{"index": i + 1, "codec": c, "channels": 2} for i, c in enumerate(audio)], "subs": []}
    if v is None:
        d["video"] = video()
    d.update(kw)
    return d


class DecideTest(unittest.TestCase):
    def mode(self, i, kind="video"):
        return playback.decide(i, kind)["mode"]

    def test_plain_files_play_directly(self):
        self.assertEqual(self.mode(info()), "direct")
        self.assertEqual(self.mode(info("matroska,webm", video("vp9"), ("opus",))), "direct")
        self.assertEqual(self.mode(info("matroska,webm", video("av1"), ("opus",))), "direct")

    def test_wrong_container_or_audio_is_only_repackaged(self):
        self.assertEqual(self.mode(info("matroska,webm", audio=("ac3",))), "remux")
        self.assertEqual(self.mode(info("avi", audio=("mp3",))), "remux")
        self.assertEqual(self.mode(info(audio=("ac3",))), "remux", "AC-3 inside MP4/MOV")

    def test_unplayable_video_is_converted(self):
        self.assertEqual(self.mode(info(v=video("hevc"))), "transcode")
        self.assertEqual(self.mode(info(v=video("prores"), audio=("pcm_s16le",))), "transcode")
        self.assertEqual(self.mode(info("mpeg", video("mpeg2video"), ("mp2",))), "transcode")
        self.assertEqual(self.mode(info(v=video(pix_fmt="yuv420p10le", profile="high 10"))), "transcode")
        self.assertEqual(self.mode(info("asf", video("wmv3"), ("wmav2",))), "transcode")

    def test_hdr_is_converted_not_copied(self):
        self.assertEqual(self.mode(info(v=video(transfer="smpte2084"))), "transcode")
        self.assertEqual(self.mode(info(v=video(transfer="arib-std-b67"))), "transcode")

    def test_rotation_prevents_direct_play(self):
        self.assertIn(self.mode(info(v=video(rotation=90))), ("remux", "transcode"))

    def test_audio_files(self):
        self.assertEqual(self.mode(info("mp3", False, ("mp3",)), "audio"), "direct")
        self.assertEqual(self.mode(info("flac", False, ("flac",)), "audio"), "direct")
        self.assertEqual(self.mode(info("asf", False, ("wmav2",)), "audio"), "transcode")

    def test_uncompressed_audio_is_direct_only_inside_wav(self):
        for codec in ("pcm_s16le", "pcm_u8", "pcm_s24le", "pcm_f32le"):
            self.assertEqual(self.mode(info("wav", False, (codec,)), "audio"), "direct", codec)
        self.assertEqual(self.mode(info("wav", False, ("pcm_s32be",)), "audio"), "transcode", "exotic PCM is converted")
        self.assertEqual(self.mode(info("aiff", False, ("pcm_s16be",)), "audio"), "transcode")
        self.assertNotEqual(self.mode(info("mov,mp4,m4a,3gp,3g2,mj2", audio=("pcm_s16le",))), "direct", "PCM inside MOV stays converted")

    def test_unreadable(self):
        self.assertEqual(playback.decide(None, "video")["mode"], "unsupported")


class ContainerLabelTest(unittest.TestCase):
    def test_mp4_family_is_told_apart_by_brand(self):
        fam = "mov,mp4,m4a,3gp,3g2,mj2"
        for brand, label in [("isom", "MPEG-4 / MP4"), ("mp42", "MPEG-4 / MP4"), ("qt  ", "QuickTime / MOV"), ("3gp4", "3GPP"),
                             ("M4A ", "MPEG-4 audio (M4A)"), ("M4V ", "MPEG-4 video (M4V)")]:
            fmt = {"format_name": fam, "format_long_name": "QuickTime / MOV", "tags": {"major_brand": brand}}
            self.assertEqual(playback.container_label(fmt), label, brand)

    def test_other_containers_keep_ffprobes_name(self):
        self.assertEqual(playback.container_label({"format_name": "matroska,webm", "format_long_name": "Matroska / WebM"}), "Matroska / WebM")
        no_brand = {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "format_long_name": "QuickTime / MOV"}
        self.assertEqual(playback.container_label(no_brand), "QuickTime / MOV", "no brand known: fall back to ffprobe's name")


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class ProbeCacheTest(IsolatedHome):
    def test_results_cached_by_an_older_version_are_read_again(self):
        from heliograph import db, views
        p = self.lib / "t.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=10", "-t", "1",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)], check=True)
        self.index()
        row = db.conn().execute("SELECT * FROM files WHERE name='t.mp4'").fetchone()
        info = playback.probe(row)
        self.assertEqual(info["v"], playback.DESCRIBE_VERSION)
        self.assertEqual(info["container_long"], "MPEG-4 / MP4")
        stale = dict(info, container_long="STALE", v=1)                     # what an older release would have stored
        db.conn().execute("UPDATE probes SET json=? WHERE file_id=?", (json.dumps(stale), row["id"]))
        self.assertEqual(playback.probe(row)["container_long"], "MPEG-4 / MP4", "the stale entry is replaced")
        stale.pop("v")
        db.conn().execute("UPDATE probes SET json=? WHERE file_id=?", (json.dumps(stale), row["id"]))
        self.assertEqual(playback.probe(row)["container_long"], "MPEG-4 / MP4", "an entry with no version is replaced too")
        self.assertEqual(playback.probe(row)["container_long"], "MPEG-4 / MP4")
        views.clear_cache()


class CandidatesTest(unittest.TestCase):
    def test_remux_copies_video_and_seeks_before_input(self):
        c = playback.candidates("/v/a b.mkv", info("matroska,webm", audio=("ac3",)), "remux", 12.5, None, 1080, "off")
        cmd = c[0]
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertLess(cmd.index("-ss"), cmd.index("-i"), "input seeking is what makes seeks fast")
        self.assertEqual(cmd[cmd.index("-i") + 1], "/v/a b.mkv", "the path is one argument, never shell-parsed")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac", "AC-3 is re-encoded to AAC")
        self.assertEqual(cmd[-1], "pipe:1")
        self.assertIn("frag_keyframe", " ".join(cmd))

    def test_transcode_has_gpu_then_cpu_fallbacks(self):
        i = info(v=video("hevc"))
        auto = playback.candidates("/v/a.mov", i, "transcode", 0, None, 720, "auto")
        self.assertEqual(auto[-1][auto[-1].index("-c:v") + 1], "libx264", "the last resort is always the CPU")
        off = playback.candidates("/v/a.mov", i, "transcode", 0, None, 720, "off")
        self.assertFalse(any("vaapi" in str(x) for cmd in off for x in cmd), "gpu=off never touches the GPU")
        for cmd in off:
            self.assertIn("scale=-2:720", " ".join(cmd))

    def test_height_is_capped_but_never_upscaled(self):
        small = info(v=video("hevc", w=640, h=360))
        c = playback.candidates("/v/a.mov", small, "transcode", 0, None, 1080, "off")
        self.assertNotIn("scale=-2:1080", " ".join(c[0]))

    def test_chosen_audio_track_is_mapped(self):
        i = info("matroska,webm", audio=("aac", "aac"))
        cmd = playback.candidates("/v/a.mkv", i, "remux", 0, 2, 1080, "off")[0]
        self.assertIn("0:2", cmd)


if __name__ == "__main__":
    unittest.main()
