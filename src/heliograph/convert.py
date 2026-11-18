"""Converting pictures, video and audio with FFmpeg (ImageMagick as a fallback for exotic pictures).

Design rules
  * Only whitelisted presets and options reach the command line - never free-form FFmpeg arguments,
    and source paths are always absolute, so a file name can never be read as an option.
  * The original is never touched. The result is written to `<name>.<ext>.part` and renamed when
    complete; an existing file is never overwritten (a " (1)" style suffix is added).
  * One conversion runs at a time, at low CPU/IO priority. Video encoding uses the GPU (VAAPI)
    when the machine has it and the preset allows it, and falls back to software on any failure.
  * Progress is real (FFmpeg's `-progress` against the probed duration) and cancelling kills FFmpeg
    and removes the partial file.
"""

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import traceback

from . import db, playback
from .meta import ffprobe_json
from .state import STATUS, USER_UMASK
from .util import ApiError, low_prefix

QUALITIES = ("high", "balanced", "small")
MAX_HEIGHTS = (0, 2160, 1440, 1080, 720, 480, 360)

IMAGE_NO_FFMPEG = frozenset("""heic heif heics heifs hif avci psd psb xcf ora kra djvu djv emf wmf eps pict pct
fits fit fts 3fr ari arw bay braw cr2 cr3 crw dcr dcs dng drf eip erf fff iiq k25 kdc mdc mef mos mrw nef nrw orf
pef ptx pxn r3d raf raw rw2 rwl rwz sr2 srf srw x3f gpr""".split())

# id: label, description, source kinds it applies to, output kind, extension, muxer
PRESETS = {
    # pictures
    "img-jpg": ("JPEG", "Smallest widely supported picture format", ("image",), "image", "jpg", "image2"),
    "img-png": ("PNG", "Lossless, keeps transparency", ("image",), "image", "png", "image2"),
    "img-webp": ("WebP", "Small files, good quality, transparency", ("image",), "image", "webp", "webp"),
    "img-avif": ("AVIF", "Much smaller than JPEG at the same quality", ("image",), "image", "avif", "avif"),
    "img-jxl": ("JPEG XL", "Modern, very efficient (few apps open it yet)", ("image",), "image", "jxl", "image2"),
    "img-tiff": ("TIFF", "Lossless archive format", ("image",), "image", "tiff", "image2"),
    "img-bmp": ("BMP", "Uncompressed bitmap", ("image",), "image", "bmp", "image2"),
    # video
    "vid-mp4-h264": ("MP4 - H.264 + AAC", "Plays on every device. Uses the GPU when available",
                     ("video",), "video", "mp4", "mp4"),
    "vid-mp4-hevc": ("MP4 - H.265 (HEVC) + AAC", "About 40% smaller than H.264; needs newer players",
                     ("video",), "video", "mp4", "mp4"),
    "vid-webm-vp9": ("WebM - VP9 + Opus", "Open format, plays in every browser", ("video",), "video", "webm", "webm"),
    "vid-webm-av1": ("WebM - AV1 + Opus", "Smallest files, slow to encode", ("video",), "video", "webm", "webm"),
    "vid-mkv-copy": ("MKV - remux, no re-encode", "Lossless and fast: only changes the container",
                     ("video",), "video", "mkv", "matroska"),
    "vid-mp4-copy": ("MP4 - remux, no re-encode", "Lossless and fast (needs H.264/H.265/AV1 inside)",
                     ("video",), "video", "mp4", "mp4"),
    "vid-mov-prores": ("MOV - ProRes 422 HQ", "Editing format, very large files", ("video",), "video", "mov", "mov"),
    "vid-gif": ("Animated GIF", "Short clips, no sound", ("video",), "video", "gif", "gif"),
    "vid-webp": ("Animated WebP", "Like GIF but smaller and better looking", ("video",), "video", "webp", "webp"),
    # audio (also "extract audio" from a video)
    "aud-mp3": ("MP3", "Plays everywhere", ("audio", "video"), "audio", "mp3", "mp3"),
    "aud-m4a": ("M4A - AAC", "Good quality at small size", ("audio", "video"), "audio", "m4a", "ipod"),
    "aud-opus": ("Opus", "Best quality per byte", ("audio", "video"), "audio", "opus", "opus"),
    "aud-ogg": ("OGG Vorbis", "Open format", ("audio", "video"), "audio", "ogg", "ogg"),
    "aud-flac": ("FLAC", "Lossless compression", ("audio", "video"), "audio", "flac", "flac"),
    "aud-wav": ("WAV", "Uncompressed", ("audio", "video"), "audio", "wav", "wav"),
}

Q_IDX = {"high": 0, "balanced": 1, "small": 2}


def presets_for(kind):
    out = []
    for pid, (label, desc, kinds, okind, ext, _) in PRESETS.items():
        if kind in kinds:
            out.append({"id": pid, "label": label, "desc": desc, "kind": okind, "ext": ext})
    return out


def api_presets():
    hw = playback.hardware()
    return {"presets": {k: presets_for(k) for k in ("image", "video", "audio")},
            "qualities": list(QUALITIES), "max_heights": list(MAX_HEIGHTS),
            "gpu": bool(hw["device"] and hw["encode_h264"]),
            "tools": {"ffmpeg": bool(shutil.which("ffmpeg")), "magick": bool(shutil.which("magick"))}}


# --------------------------------------------------------------------------- command building

def _q(values, quality):
    return values[Q_IDX.get(quality, 1)]


def _image_cmds(spec, src, dst, info):
    pid, quality = spec["preset"], spec.get("quality", "balanced")
    label, _, _, _, ext, muxer = PRESETS[pid]
    ms = int(spec.get("max_size") or 0)
    codec = {
        "jpg": ["-c:v", "mjpeg", "-q:v", str(_q((3, 6, 11), quality)), "-pix_fmt", "yuvj420p"],
        "png": ["-c:v", "png", "-compression_level", str(_q((6, 6, 9), quality))],
        "webp": ["-c:v", "libwebp", "-quality", str(_q((95, 82, 65), quality)), "-compression_level", "4"],
        "avif": ["-c:v", "libaom-av1", "-crf", str(_q((20, 28, 36), quality)), "-cpu-used", "6", "-still-picture", "1",
                 "-pix_fmt", "yuv420p"],
        "jxl": ["-c:v", "libjxl", "-distance", str(_q((0.5, 1.0, 2.5), quality)), "-effort", "5"],
        "tiff": ["-c:v", "tiff", "-compression_algo", "lzw"],
        "bmp": ["-c:v", "bmp"],
    }[ext]
    chain = []
    if ms > 0:
        chain.append(f"scale='min({ms},iw)':'min({ms},ih)':force_original_aspect_ratio=decrease")
    if ext in ("jpg", "bmp"):                            # no alpha in the target: put the picture on white
        chain.append("split[a][b];[a]drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill[bg];[bg][b]overlay=format=auto")
    ff = [shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-v", "error", "-hide_banner", "-y", "-progress", "pipe:1",
          "-nostats", "-i", src]
    if chain:
        ff += ["-vf", ",".join(chain)]
    ff += ["-frames:v", "1", *codec]
    if spec.get("strip"):
        ff += ["-map_metadata", "-1"]
    ff += ["-f", muxer] + (["-update", "1"] if muxer == "image2" else []) + [dst]
    cmds = [ff]
    mg = shutil.which("magick")
    if mg:                                               # fallback decoder for HEIC / RAW / PSD / ...
        m = [mg, "-limit", "memory", "512MiB", "-limit", "map", "1GiB", f"{src}[0]", "-auto-orient"]
        if ms > 0:
            m += ["-resize", f"{ms}x{ms}>"]
        if ext in ("jpg", "bmp"):
            m += ["-background", "white", "-alpha", "remove", "-alpha", "off"]
        if spec.get("strip"):
            m += ["-strip"]
        if ext in ("jpg", "webp", "avif"):
            m += ["-quality", str(_q((95, 85, 70), quality))]
        cmds.append(m + [f"{ext}:{dst}"])
        if src.rsplit(".", 1)[-1].lower() in IMAGE_NO_FFMPEG:
            cmds.reverse()                               # FFmpeg cannot read these: try ImageMagick first
    return cmds


def _scale_sw(max_h, src_h):
    if max_h and src_h and src_h > max_h:
        return f"scale=-2:{max_h - max_h % 2}"
    return "scale=trunc(iw/2)*2:trunc(ih/2)*2"


def _video_cmds(spec, src, dst, info):
    pid, quality = spec["preset"], spec.get("quality", "balanced")
    label, _, _, _, ext, muxer = PRESETS[pid]
    exe = shutil.which("ffmpeg") or "ffmpeg"
    head = [exe, "-nostdin", "-v", "error", "-hide_banner", "-y", "-progress", "pipe:1", "-nostats"]
    v = (info or {}).get("video") or {}
    max_h = int(spec.get("max_height") or 0)
    sh = v.get("h") or 0
    meta = ["-map_metadata", "-1"] if spec.get("strip") else ["-map_metadata", "0"]
    tail = ["-f", muxer, dst]
    maps = ["-map", "0:v:0", "-map", "0:a?"]
    if pid in ("vid-mkv-copy", "vid-mp4-copy"):
        mp = maps + (["-map", "0:s?"] if pid == "vid-mkv-copy" else [])
        extra = ["-movflags", "+faststart"] if ext == "mp4" else []
        codecs = ["-c", "copy"]
        first_a = ((info or {}).get("audio") or [{}])[0].get("codec")
        if ext == "mp4" and first_a and first_a not in ("aac", "mp3"):
            codecs += ["-c:a", "aac", "-b:a", "192k"]      # keep the picture untouched, make the sound playable
        return [head + ["-i", src, *mp, *codecs, *meta, *extra, *tail]]
    if pid in ("vid-gif", "vid-webp"):
        fps = _q((15, 12, 10), quality)
        if max_h:                                              # an explicit height limit wins ...
            scale = f"scale=w=-2:h='min({max_h},ih)':flags=lanczos"
        else:                                                  # ... otherwise a width cap keeps animations small
            scale = f"scale=w='min({_q((800, 640, 480), quality)},iw)':h=-2:flags=lanczos"
        if pid == "vid-gif":
            fg = (f"fps={fps},{scale},split[s0][s1];[s0]palettegen=stats_mode=diff[p];"
                  "[s1][p]paletteuse=dither=bayer:bayer_scale=5")
            return [head + ["-i", src, "-an", "-vf", fg, "-loop", "0", *tail]]
        return [head + ["-i", src, "-an", "-vf", f"fps={fps},{scale}",
                        "-c:v", "libwebp_anim", "-quality", str(_q((85, 70, 55), quality)), "-loop", "0", *tail]]
    sw_filter = _scale_sw(max_h, sh) + ",format=yuv420p"
    a_aac = ["-c:a", "aac", "-b:a", _q(("192k", "160k", "128k"), quality)]
    a_opus = ["-c:a", "libopus", "-b:a", _q(("160k", "128k", "96k"), quality)]
    cmds = []
    if pid == "vid-mp4-h264":
        hw = playback.hardware()
        if spec.get("gpu", "auto") != "off" and hw["device"] and hw["encode_h264"]:
            qp = str(_q((20, 24, 29), quality))
            if v.get("codec") in hw["decode"] and v.get("pix_fmt") in ("yuv420p", "yuvj420p", "yuv420p10le"):
                scale = (f"scale_vaapi=w=-2:h={max_h - max_h % 2}:format=nv12" if max_h and sh > max_h
                         else "scale_vaapi=format=nv12")
                cmds.append(head + ["-hwaccel", "vaapi", "-hwaccel_device", hw["device"],
                                    "-hwaccel_output_format", "vaapi",
                                    "-i", src, *maps, "-vf", scale, "-c:v", "h264_vaapi", "-qp", qp, *a_aac, *meta,
                             "-movflags", "+faststart", *tail])
            cmds.append(head + ["-init_hw_device", f"vaapi=va:{hw['device']}", "-filter_hw_device", "va",
                         "-i", src, *maps, "-vf", _scale_sw(max_h, sh) + ",format=nv12,hwupload", "-c:v", "h264_vaapi",
                         "-qp", qp, *a_aac, *meta, "-movflags", "+faststart", *tail])
        cmds.append(head + ["-i", src, *maps, "-vf", sw_filter, "-c:v", "libx264", "-preset", "medium",
                            "-crf", str(_q((18, 22, 27), quality)), "-profile:v", "high", *a_aac, *meta,
                            "-movflags", "+faststart", *tail])
    elif pid == "vid-mp4-hevc":
        hw = playback.hardware()
        if spec.get("gpu", "auto") != "off" and hw["device"] and "hevc" in hw["decode"]:
            cmds.append(head + ["-init_hw_device", f"vaapi=va:{hw['device']}", "-filter_hw_device", "va",
                         "-i", src, *maps, "-vf", _scale_sw(max_h, sh) + ",format=nv12,hwupload", "-c:v", "hevc_vaapi",
                         "-qp", str(_q((24, 28, 33), quality)), "-tag:v", "hvc1", *a_aac, *meta,
                         "-movflags", "+faststart", *tail])
        cmds.append(head + ["-i", src, *maps, "-vf", sw_filter, "-c:v", "libx265", "-preset", "medium",
                            "-crf", str(_q((20, 25, 30), quality)), "-tag:v", "hvc1", *a_aac, *meta,
                            "-movflags", "+faststart", *tail])
    elif pid == "vid-webm-vp9":
        cmds.append(head + ["-i", src, *maps, "-vf", sw_filter, "-c:v", "libvpx-vp9", "-b:v", "0",
                            "-crf", str(_q((26, 32, 38), quality)), "-row-mt", "1", "-cpu-used", "2",
                            *a_opus, *meta, *tail])
    elif pid == "vid-webm-av1":
        cmds.append(head + ["-i", src, *maps, "-vf", sw_filter, "-c:v", "libsvtav1",
                            "-crf", str(_q((26, 32, 38), quality)), "-preset", "8", *a_opus, *meta, *tail])
    elif pid == "vid-mov-prores":
        cmds.append(head + ["-i", src, *maps, "-vf", _scale_sw(max_h, sh) + ",format=yuv422p10le",
                            "-c:v", "prores_ks", "-profile:v", "3", "-vendor", "apl0", "-c:a", "pcm_s16le",
                            *meta, *tail])
    return cmds


def _audio_cmds(spec, src, dst, info):
    pid, quality = spec["preset"], spec.get("quality", "balanced")
    label, _, _, _, ext, muxer = PRESETS[pid]
    head = [shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-v", "error", "-hide_banner", "-y", "-progress", "pipe:1",
            "-nostats", "-i", src, "-map", "0:a:0", "-vn"]
    codec = {
        "mp3": ["-c:a", "libmp3lame", "-q:a", str(_q((0, 2, 5), quality)), "-id3v2_version", "3"],
        "m4a": ["-c:a", "aac", "-b:a", _q(("256k", "192k", "128k"), quality)],
        "opus": ["-c:a", "libopus", "-b:a", _q(("160k", "128k", "80k"), quality)],
        "ogg": ["-c:a", "libvorbis", "-q:a", str(_q((7, 5, 3), quality))],
        "flac": ["-c:a", "flac", "-compression_level", str(_q((8, 5, 2), quality))],
        "wav": ["-c:a", "pcm_s16le"],
    }[ext]
    meta = ["-map_metadata", "-1"] if spec.get("strip") else ["-map_metadata", "0"]
    return [head + codec + meta + ["-f", muxer, dst]]


# --------------------------------------------------------------------------- naming / validation

def unique_path(directory, stem, ext):
    base = os.path.join(directory, f"{stem}.{ext}")
    n = 0
    while os.path.exists(base) or os.path.exists(base + ".part"):
        n += 1
        base = os.path.join(directory, f"{stem} ({n}).{ext}")
    return base


def validate_spec(spec, kind):
    pid = spec.get("preset")
    if pid not in PRESETS:
        raise ApiError(400, "Unknown conversion preset")
    if kind not in PRESETS[pid][2]:
        raise ApiError(400, f"{PRESETS[pid][0]} cannot be made from a {kind} file")
    if spec.get("quality", "balanced") not in QUALITIES:
        raise ApiError(400, "Unknown quality")
    try:
        mh = int(spec.get("max_height") or 0)
        ms = int(spec.get("max_size") or 0)
    except (TypeError, ValueError):
        raise ApiError(400, "Bad size limit")
    if mh not in MAX_HEIGHTS or not (0 <= ms <= 20000):
        raise ApiError(400, "Bad size limit")
    if pid.endswith("-copy") and mh:
        raise ApiError(400, "A remux copies the video as it is, so it cannot be resized. Pick a re-encoding format")
    if spec.get("gpu", "auto") not in ("auto", "off"):
        raise ApiError(400, "Bad GPU setting")
    out = spec.get("out_dir")
    if out:
        from .views import validate_dir
        validate_dir(out)
        if not os.access(out, os.W_OK):
            raise ApiError(403, "That folder is not writable")


# --------------------------------------------------------------------------- running

class Cancelled(Exception):
    pass


def run_ffmpeg(cmd, duration, progress, cancel):
    """Run one command; returns (returncode, last stderr lines). Raises Cancelled."""
    proc = subprocess.Popen(low_prefix(10) + cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True, text=True, errors="replace")
    tail = []

    def drain():
        for line in proc.stderr:
            tail.append(line.rstrip())
            del tail[:-12]

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        for line in proc.stdout:
            if cancel.is_set():
                raise Cancelled()
            if line.startswith("out_time_us=") and duration:
                try:
                    progress(min(0.99, int(line.split("=")[1]) / 1e6 / duration))
                except ValueError:
                    pass
        proc.wait()
    except Cancelled:
        try:
            os.killpg(proc.pid, 15)
        except OSError:
            pass
        proc.wait()
        raise
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, 15)
            except OSError:
                pass
            proc.wait()
        reader.join(timeout=2)
        for pipe in (proc.stdout, proc.stderr):          # close deterministically: a busy service must not leak descriptors
            try:
                pipe.close()
            except OSError:
                pass
    return proc.returncode, tail


def run_spec(spec, progress=lambda f: None, cancel=None):
    """Convert one file. Returns {"dst": path, "bytes_in": n, "bytes_out": n}. Raises RuntimeError on failure."""
    cancel = cancel or threading.Event()
    src = os.path.abspath(spec["src"])
    kind = spec["kind"]
    pid = spec["preset"]
    label, _, _, okind, ext, muxer = PRESETS[pid]
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed")
    out_dir = spec.get("out_dir") or os.path.dirname(src)
    stem = os.path.splitext(os.path.basename(src))[0]
    if os.path.splitext(src)[1].lower().lstrip(".") == ext and not spec.get("out_dir"):
        stem += " (converted)"
    dst = unique_path(out_dir, stem, ext)
    part = dst + ".part"
    info, duration = None, 0.0
    if kind in ("video", "audio"):
        j = ffprobe_json(src, timeout=40)
        if not j:
            raise RuntimeError("The file could not be read (is it damaged?)")
        info = playback.describe(j)
        duration = info.get("dur") or 0.0
        if pid.startswith("aud-") and not info.get("audio"):
            raise RuntimeError("This file has no audio track")
    cmds = {"image": _image_cmds, "video": _video_cmds, "audio": _audio_cmds}[okind](spec, src, part, info)
    last_err = ""
    try:
        for cmd in cmds:
            if cancel.is_set():
                raise Cancelled()
            if os.path.exists(part):
                os.unlink(part)
            if cmd[0].endswith("magick"):
                rc, tail = run_ffmpeg_like(cmd, cancel)
            else:
                rc, tail = run_ffmpeg(cmd, duration, progress, cancel)
            if rc == 0 and os.path.exists(part) and os.path.getsize(part) > 0:
                os.replace(part, dst)
                try:
                    os.chmod(dst, 0o666 & ~USER_UMASK[0])        # a converted file is the user's, not private to the service
                except OSError:
                    pass
                progress(1.0)
                return {"dst": dst, "bytes_in": os.path.getsize(src), "bytes_out": os.path.getsize(dst)}
            last_err = " ".join(tail[-3:])
        raise RuntimeError(last_err or "FFmpeg could not convert this file")
    finally:
        if os.path.exists(part):
            try:
                os.unlink(part)
            except OSError:
                pass


def run_ffmpeg_like(cmd, cancel):
    """Run a non-FFmpeg converter (ImageMagick) with the same cancel behaviour."""
    proc = subprocess.Popen(low_prefix(10) + cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, start_new_session=True, text=True, errors="replace")
    while True:
        if cancel.is_set():
            try:
                os.killpg(proc.pid, 15)
            except OSError:
                pass
            proc.wait()
            proc.stderr.close()
            raise Cancelled()
        try:
            err = proc.communicate(timeout=0.5)[1]
            return proc.returncode, (err or "").strip().splitlines()[-3:]
        except subprocess.TimeoutExpired:
            continue


# --------------------------------------------------------------------------- job queue

class JobManager:
    def __init__(self, on_done=None):
        self.q = queue.Queue()
        self.cancel_events = {}
        self.current = None
        self.on_done = on_done
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="convert")
        self.stop_flag = threading.Event()

    def start(self):
        c = db.conn()
        c.execute("UPDATE jobs SET status='failed', message='Interrupted (the service was stopped)', finished=? "
                  "WHERE status='running'", (int(time.time()),))
        for r in c.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY id").fetchall():
            self.q.put(r["id"])
        self.thread.start()
        self._count()

    def stop(self):
        self.stop_flag.set()
        with self.lock:
            for ev in self.cancel_events.values():
                ev.set()

    def _count(self):
        c = db.conn()
        STATUS["jobs"]["running"] = c.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0]
        STATUS["jobs"]["queued"] = c.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]

    def submit(self, items, spec_common):
        """items: [(file row)] -> list of new job ids. Validates everything before queueing anything."""
        specs = []
        for r in items:
            spec = dict(spec_common)
            validate_spec(spec, r["kind"])
            spec.update(src=r["path"], kind=r["kind"])
            specs.append(spec)
        c = db.conn()
        ids = []
        now = int(time.time())
        c.execute("BEGIN IMMEDIATE")
        for spec in specs:
            cur = c.execute("INSERT INTO jobs(created,status,kind,preset,params,src,progress) VALUES(?,?,?,?,?,?,0)",
                            (now, "queued", spec["kind"], spec["preset"], json.dumps(
                                {k: spec.get(k) for k in ("quality", "max_height", "max_size", "gpu", "strip", "out_dir")}),
                             spec["src"]))
            ids.append(cur.lastrowid)
        c.execute("COMMIT")
        for i in ids:
            self.q.put(i)
        self._count()
        return ids

    def cancel(self, job_id):
        c = db.conn()
        r = c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not r:
            raise ApiError(404, "No such job")
        if r["status"] == "queued":
            c.execute("UPDATE jobs SET status='cancelled', finished=? WHERE id=? AND status='queued'",
                      (int(time.time()), job_id))
        elif r["status"] == "running":
            with self.lock:
                ev = self.cancel_events.get(job_id)
            if ev:
                ev.set()
        self._count()

    def _loop(self):
        try:
            while not self.stop_flag.is_set():
                try:
                    jid = self.q.get(timeout=1.0)
                except queue.Empty:
                    continue
                try:
                    self._run(jid)
                except Exception:
                    traceback.print_exc()
        finally:
            db.close_thread_connection()

    def _run(self, jid):
        c = db.conn()
        r = c.execute("SELECT * FROM jobs WHERE id=? AND status='queued'", (jid,)).fetchone()
        if not r:
            return
        params = json.loads(r["params"] or "{}")
        spec = dict(params, src=r["src"], kind=r["kind"], preset=r["preset"])
        ev = threading.Event()
        with self.lock:
            self.cancel_events[jid] = ev
        c.execute("UPDATE jobs SET status='running', started=? WHERE id=?", (int(time.time()), jid))
        self._count()
        last = [0.0]

        def progress(f):
            if f - last[0] >= 0.01 or f >= 1.0:
                last[0] = f
                c.execute("UPDATE jobs SET progress=? WHERE id=?", (round(f, 4), jid))

        try:
            res = run_spec(spec, progress, ev)
            c.execute("UPDATE jobs SET status='done', progress=1, finished=?, dst=?, bytes_in=?, bytes_out=?, message=NULL "
                      "WHERE id=?", (int(time.time()), res["dst"], res["bytes_in"], res["bytes_out"], jid))
            if self.on_done:
                self.on_done(res["dst"])
        except Cancelled:
            c.execute("UPDATE jobs SET status='cancelled', finished=? WHERE id=?", (int(time.time()), jid))
        except Exception as e:
            c.execute("UPDATE jobs SET status='failed', finished=?, message=? WHERE id=?",
                      (int(time.time()), str(e)[:400], jid))
        finally:
            with self.lock:
                self.cancel_events.pop(jid, None)
            self._count()


def api_jobs(limit=60):
    rows = db.conn().execute("SELECT id,created,started,finished,status,kind,preset,src,dst,progress,message,"
                             "bytes_in,bytes_out FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return {"jobs": [dict(r) for r in rows]}


def clear_finished():
    db.conn().execute("DELETE FROM jobs WHERE status IN ('done','failed','cancelled')")
