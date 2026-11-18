"""Making any video or audio file playable in a Chromium window.

A Chromium based window decodes H.264/VP9/AV1 with AAC/MP3/Opus/Vorbis/FLAC. Everything else -
HEVC (iPhone .mov), ProRes, MPEG-2, WMV, DivX, 10-bit H.264, AC-3/DTS/ALAC audio, AVI/TS/FLV
containers - needs help. For each file we probe the real streams once (cached in the database) and
pick the cheapest way to show it:

  direct     the browser plays the original file (Range requests, instant seeking, zero CPU)
  remux      copy the video as is, only change the container (and the audio if needed): ~1% CPU
  transcode  re-encode to H.264/AAC: on the GPU through VAAPI when this machine has it (low CPU),
             otherwise libx264 at a modest preset; 4K is scaled down to the configured maximum
             height and HDR is tone-mapped to SDR

Remux and transcode are streamed as fragmented MP4 straight from FFmpeg's pipe; seeking restarts
the stream at the new position (the player knows the duration from the probe). Nothing is written
to disk, and FFmpeg stops the moment the window stops reading.
"""

import json
import os
import select
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from . import db
from .meta import ffprobe_json
from .state import STATUS
from .util import low_prefix

V_DIRECT = {"h264", "vp8", "vp9", "av1", "theora"}
A_DIRECT = {"aac", "mp3", "opus", "vorbis", "flac"}
A_PCM_WAV = {"pcm_s16le", "pcm_u8", "pcm_s24le", "pcm_f32le"}   # uncompressed audio a browser plays inside a WAV file
V_MP4_COPY = {"h264", "vp9", "av1"}                  # may be stream-copied into fragmented MP4
CONTAINER_OK = ("mov", "mp4", "m4a", "3gp", "matroska", "webm", "ogg", "mp3", "flac", "wav")
H264_OK_PROFILES = ("baseline", "constrained baseline", "main", "high", "extended")
HDR_TRANSFERS = ("smpte2084", "arib-std-b67")
SUB_EXT = (".srt", ".vtt", ".ass", ".ssa", ".sub")


# --------------------------------------------------------------------------- probing

# Bump this whenever describe() starts producing different output: cached results of older versions are re-read.
DESCRIBE_VERSION = 2


def container_label(fmt):
    """A human name for the container. ffprobe gives one name ("QuickTime / MOV") to the whole MP4 family, so the
    file's own major brand is used to tell MP4, M4A, 3GPP and QuickTime apart."""
    names = (fmt.get("format_name") or "").split(",")
    if "mp4" in names:
        brand = ((fmt.get("tags") or {}).get("major_brand") or "").strip().lower()
        if brand == "qt":
            return "QuickTime / MOV"
        if brand.startswith("3g"):
            return "3GPP"
        if brand in ("m4a", "m4b"):
            return "MPEG-4 audio (M4A)"
        if brand == "m4v":
            return "MPEG-4 video (M4V)"
        if brand:
            return "MPEG-4 / MP4"
    return fmt.get("format_long_name", "")


def describe(j, path=""):
    """A compact, UI-friendly description of ffprobe's JSON."""
    fmt = j.get("format") or {}
    streams = j.get("streams") or []
    vids = [s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")]
    v = vids[0] if vids else None
    out = {"v": DESCRIBE_VERSION, "container": fmt.get("format_name", ""), "container_long": container_label(fmt),
           "dur": _f(fmt.get("duration")), "bit_rate": _i(fmt.get("bit_rate")), "size": _i(fmt.get("size")),
           "video": None, "audio": [], "subs": [], "has_cover": any(
               s.get("codec_type") == "video" and (s.get("disposition") or {}).get("attached_pic") for s in streams)}
    if v:
        rot = (v.get("tags") or {}).get("rotate")
        for sd in v.get("side_data_list") or []:
            rot = sd.get("rotation", rot)
        out["video"] = {
            "index": v.get("index"), "codec": v.get("codec_name"), "profile": (v.get("profile") or "").lower(),
            "pix_fmt": v.get("pix_fmt"), "w": v.get("width"), "h": v.get("height"), "fps": _rate(v.get("avg_frame_rate")),
            "bit_rate": _i(v.get("bit_rate")), "transfer": v.get("color_transfer"), "primaries": v.get("color_primaries"),
            "field_order": v.get("field_order"), "rotation": _i(rot) or 0,
            "bits": _i(v.get("bits_per_raw_sample"))}
    for s in streams:
        t = s.get("codec_type")
        tags = {str(k).lower(): val for k, val in (s.get("tags") or {}).items()}
        if t == "audio":
            out["audio"].append({"index": s.get("index"), "codec": s.get("codec_name"), "channels": s.get("channels"),
                                 "rate": _i(s.get("sample_rate")), "lang": tags.get("language"), "title": tags.get("title"),
                                 "default": bool((s.get("disposition") or {}).get("default")),
                                 "bit_rate": _i(s.get("bit_rate"))})
        elif t == "subtitle":
            out["subs"].append({"index": s.get("index"), "codec": s.get("codec_name"), "lang": tags.get("language"),
                                "title": tags.get("title"),
                                "text": s.get("codec_name") in ("subrip", "ass", "ssa", "webvtt", "mov_text", "srt", "text")})
    if path:
        stem = os.path.splitext(os.path.basename(path))[0]
        d = os.path.dirname(path)
        try:
            for name in sorted(os.listdir(d))[:4000]:
                low = name.lower()
                if name.startswith(stem) and low.endswith(SUB_EXT) and os.path.isfile(os.path.join(d, name)):
                    out["subs"].append({"index": None, "file": name, "codec": low.rsplit(".", 1)[1],
                                        "lang": None, "title": name[len(stem):].strip(". ") or "external", "text": True})
        except OSError:
            pass
    return out


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _rate(v):
    try:
        a, b = str(v).split("/")
        return round(float(a) / float(b), 3) if float(b) else None
    except (TypeError, ValueError):
        return None


def probe(row):
    """Stream details of a file (cached in the database until the file changes). None if unreadable."""
    c = db.conn()
    r = c.execute("SELECT json FROM probes WHERE file_id=? AND mtime=? AND size=?",
                  (row["id"], row["mtime"], row["size"])).fetchone()
    if r:
        try:
            cached = json.loads(r[0])
            if cached.get("v") == DESCRIBE_VERSION:
                return cached
        except (ValueError, AttributeError):
            pass
    j = ffprobe_json(row["path"], timeout=40)
    if not j:
        return None
    info = describe(j, row["path"])
    c.execute("INSERT OR REPLACE INTO probes(file_id, mtime, size, json) VALUES(?,?,?,?)",
              (row["id"], row["mtime"], row["size"], json.dumps(info, separators=(",", ":"))))
    return info


# --------------------------------------------------------------------------- the decision

def _video_direct(v):
    if not v:
        return True
    codec, pix, prof = v.get("codec"), v.get("pix_fmt") or "", v.get("profile") or ""
    if codec not in V_DIRECT:
        return False
    if codec == "h264":
        return (pix in ("yuv420p", "yuvj420p", "")) and (not prof or prof in H264_OK_PROFILES)
    if codec in ("vp9", "av1"):
        return pix in ("yuv420p", "yuv420p10le", "yuvj420p", "")
    return True


def _audio_direct(codec, container):
    """May the browser play this audio codec as it is? (PCM only inside WAV: in MOV/MP4 it is not reliable.)"""
    return codec in A_DIRECT or (codec in A_PCM_WAV and "wav" in container.split(","))


def decide(info, kind):
    """-> {"mode": direct|remux|transcode, "why": str}"""
    if not info:
        return {"mode": "unsupported", "why": "The file could not be read by ffprobe"}
    cont = info.get("container", "")
    cont_ok = any(c in cont.split(",") for c in CONTAINER_OK)
    v = info.get("video")
    audio = info.get("audio") or []
    first_a = audio[0]["codec"] if audio else None
    a_ok = first_a is None or _audio_direct(first_a, cont)
    if kind == "audio" or v is None:
        if a_ok and cont_ok:
            return {"mode": "direct", "why": "Plays as is"}
        return {"mode": "transcode", "why": f"Audio ({first_a}) is converted for playback"}
    if first_a is not None and "mp4" in cont.split(",") and first_a not in ("aac", "mp3", "opus", "flac"):
        a_ok = False                                      # e.g. AC-3 / ALAC / PCM inside MP4 or MOV
    v_ok = _video_direct(v)
    hdr = (v.get("transfer") in HDR_TRANSFERS)
    if v_ok and a_ok and cont_ok and not v.get("rotation") and not hdr:
        return {"mode": "direct", "why": "Plays as is"}
    if v_ok and v.get("codec") in V_MP4_COPY and not hdr:
        bits = []
        if not cont_ok:
            bits.append(f"container {cont.split(',')[0]}")
        if not a_ok:
            bits.append(f"audio {first_a}")
        if v.get("rotation"):
            bits.append("rotation")
        return {"mode": "remux", "why": "Repackaged for playback (" + ", ".join(bits) + "); the video is not re-encoded"}
    reason = f"{(v.get('codec') or '?').upper()}" + (f" {v.get('pix_fmt')}" if v.get("pix_fmt") else "")
    if hdr:
        reason += " HDR"
    return {"mode": "transcode", "why": f"{reason} is converted to H.264 for playback"}


def keyframe_at(path, t):
    """Where a stream copy that starts at `t` really begins: the key frame at or before `t`.
    (Copying cannot start between key frames, so the player needs the true start to keep its clock right.)"""
    exe = shutil.which("ffprobe")
    if not exe or t <= 0:
        return max(0.0, t)
    cmd = low_prefix(5) + [exe, "-v", "error", "-select_streams", "v:0", "-read_intervals", f"{t:.3f}%+#1",
                           "-show_entries", "packet=pts_time", "-of", "csv=p=0", path]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return t
    for line in out.splitlines():
        try:
            return max(0.0, min(t, float(line.split(",")[0])))
        except ValueError:
            continue
    return t


# --------------------------------------------------------------------------- hardware

_hw = {"done": False}
_hw_lock = threading.Lock()


def hardware():
    """VAAPI capabilities, detected once: device path, decodable codecs, whether H.264 encoding works."""
    with _hw_lock:
        if _hw["done"]:
            return _hw
        _hw.update(done=True, device=None, decode=set(), encode_h264=False)
        vainfo, ffmpeg = shutil.which("vainfo"), shutil.which("ffmpeg")
        dev = next((str(p) for p in sorted(Path("/dev/dri").glob("renderD*"))), None) if Path("/dev/dri").is_dir() else None
        if not (dev and ffmpeg):
            return _hw
        enc = False
        dec = set()
        if vainfo:
            try:
                out = subprocess.run([vainfo, "--display", "drm", "--device", dev], capture_output=True,
                                     text=True, timeout=10).stdout
            except (OSError, subprocess.TimeoutExpired):
                out = ""
            names = {"H264": "h264", "HEVC": "hevc", "VP9": "vp9", "VP8": "vp8", "MPEG2": "mpeg2video",
                     "VC1": "vc1", "AV1": "av1", "JPEG": "mjpeg"}
            for line in out.splitlines():
                line = line.strip()
                if not line.startswith("VAProfile"):
                    continue
                prof, _, ent = line.partition(":")
                prof = prof.replace("VAProfile", "")
                for k, v in names.items():
                    if prof.startswith(k):
                        if "VAEntrypointVLD" in ent:
                            dec.add(v)
                        if k == "H264" and "VAEntrypointEncSlice" in ent:
                            enc = True
        _hw.update(device=dev, decode=dec, encode_h264=enc)
        return _hw


# --------------------------------------------------------------------------- command lines

def _tonemap(v):
    """HDR (PQ / HLG) to SDR: linearize with the stated input colour space, tone-map, convert to BT.709."""
    tin = v.get("transfer") or "smpte2084"
    prim = v.get("primaries") if v.get("primaries") in ("bt2020", "smpte432", "smpte431", "bt709") else "bt2020"
    mat = "bt2020nc" if prim == "bt2020" else "bt709"
    return (f"zscale=tin={tin}:pin={prim}:min={mat}:t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
            "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv")


def candidates(src, info, mode, start, audio_index, max_height, gpu):
    """FFmpeg command lines to try, best first. Each streams fragmented MP4 to stdout."""
    v = info.get("video") or {}
    audio = info.get("audio") or []
    a = next((x for x in audio if x["index"] == audio_index), audio[0] if audio else None)
    sh = v.get("h") or 0
    rot = v.get("rotation") or 0
    if rot in (90, 270, -90, -270):
        sh = v.get("w") or sh
    target_h = min(max_height, sh) if sh else max_height
    target_h -= target_h % 2
    hdr = v.get("transfer") in HDR_TRANSFERS
    interlaced = (v.get("field_order") or "progressive") not in ("progressive", "unknown")
    head = ["-nostdin", "-v", "error", "-hide_banner"]
    sw_threads = ["-threads", str(max(2, min(4, (os.cpu_count() or 4) // 2)))]
    seek = ["-ss", f"{start:.3f}"] if start and start > 0 else []
    mux = ["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"]
    a_args = []
    if a is not None:
        if mode == "remux" and a["codec"] == "aac":
            a_args = ["-map", f"0:{a['index']}", "-c:a", "copy"]
        else:
            a_args = ["-map", f"0:{a['index']}", "-c:a", "aac", "-b:a", "160k", "-ac", "2",
                      "-af", "aresample=async=1:first_pts=0"]
    out = []
    exe = shutil.which("ffmpeg") or "ffmpeg"
    if info.get("video") is None:                                   # audio only
        out.append([exe, *head, *seek, "-i", src, "-vn", "-sn", "-dn", *a_args, *mux])
        return out
    vmap = ["-map", f"0:{v['index']}"]
    if mode == "remux":
        out.append([exe, *head, *seek, "-i", src, *vmap, "-c:v", "copy", *a_args, "-sn", "-dn", *mux])
        return out
    # --- transcode
    filt = []
    if interlaced:
        filt.append("yadif=mode=0:deint=interlaced")
    filt.append(f"scale=-2:{target_h}" if target_h and target_h < sh else "scale=trunc(iw/2)*2:trunc(ih/2)*2")
    if hdr:
        filt.append(_tonemap(v))
    hw = hardware()
    use_hw = gpu != "off" and hw["device"] and hw["encode_h264"]
    if use_hw and not hdr and not interlaced and v.get("codec") in hw["decode"] and v.get("pix_fmt") in (
            "yuv420p", "yuvj420p", "yuv420p10le", None):
        scale = f"scale_vaapi=w=-2:h={target_h}:format=nv12" if target_h and target_h < sh else "scale_vaapi=format=nv12"
        out.append([exe, *head, "-hwaccel", "vaapi", "-hwaccel_device", hw["device"], "-hwaccel_output_format", "vaapi",
                    *seek, "-i", src, *vmap, "-vf", scale, "-c:v", "h264_vaapi", "-qp", "24", "-g", "60",
                    *a_args, "-sn", "-dn", *mux])
    if use_hw:
        out.append([exe, *head, "-init_hw_device", f"vaapi=va:{hw['device']}", "-filter_hw_device", "va",
                    *sw_threads, *seek, "-i", src, *vmap, "-vf", ",".join(filt + ["format=nv12", "hwupload"]),
                    "-c:v", "h264_vaapi", "-qp", "24", "-g", "60", *a_args, "-sn", "-dn", *mux])
    def x264(chain):
        return [exe, *head, *sw_threads, *seek, "-i", src, *vmap, "-vf", ",".join(chain + ["format=yuv420p"]),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-profile:v", "high", "-g", "60",
                "-sc_threshold", "0", "-threads", str(min(4, (os.cpu_count() or 2))), *a_args, "-sn", "-dn", *mux]

    out.append(x264(filt))
    if hdr:                                           # last resort: skip tone-mapping (washed-out but it plays)
        out.append(x264([f for f in filt if not f.startswith("zscale")]))
    return out


# --------------------------------------------------------------------------- streaming

_streams = []
_streams_lock = threading.Lock()
MAX_STREAMS = 2


def _register(proc):
    with _streams_lock:
        _streams.append(proc)
        while len(_streams) > MAX_STREAMS:              # an abandoned seek: stop the oldest one
            old = _streams.pop(0)
            _kill(old)
        STATUS["stream"]["active"] = len(_streams)


def _unregister(proc):
    with _streams_lock:
        if proc in _streams:
            _streams.remove(proc)
        STATUS["stream"]["active"] = len(_streams)


def _kill(proc):
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, 15)
        except OSError:
            try:
                proc.terminate()
            except OSError:
                pass


def stop_all_streams():
    with _streams_lock:
        procs, _streams[:] = list(_streams), []
        STATUS["stream"]["active"] = 0
    for p in procs:
        _kill(p)


def _client_closed(sock):
    """True when the browser has closed its end of the connection (it stopped waiting for us)."""
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        if not readable:
            return False
        return sock.recv(1, socket.MSG_PEEK) == b""      # readable + empty = EOF; data = a pipelined request
    except (OSError, ValueError):
        return True


def stream_to(handler, cmds, label=""):
    """Run the first FFmpeg command that produces output and pipe it to the HTTP client.
    Returns the command index that worked, or -1 if none did (nothing has been sent in that case)."""
    sock = handler.connection
    errors = []
    for idx, cmd in enumerate(cmds):
        full = low_prefix(5) + cmd
        try:
            proc = subprocess.Popen(full, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True)
        except OSError as e:
            errors.append(str(e))
            continue
        _register(proc)
        err_buf = []

        def drain(p=proc, buf=err_buf):
            try:
                for line in p.stderr:
                    buf.append(line.decode("utf-8", "replace").rstrip())
                    del buf[:-12]
            except (OSError, ValueError):
                pass

        threading.Thread(target=drain, daemon=True).start()
        fd = proc.stdout.fileno()
        first = b""
        deadline = time.time() + 25
        while time.time() < deadline:
            r, _, _ = select.select([fd, sock], [], [], 0.5)
            if sock in r and _client_closed(sock):
                _kill(proc)
                _unregister(proc)
                return -2
            if fd in r:
                first = os.read(fd, 65536)
                break
            if proc.poll() is not None:
                break
        if not first:
            _kill(proc)
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                pass
            _unregister(proc)
            errors.append(f"candidate {idx}: " + " | ".join(err_buf[-3:]))
            continue
        try:
            handler.send_response(200)
            handler.send_header("Content-Type", "video/mp4" if "-vn" not in cmd else "audio/mp4")
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Accept-Ranges", "none")
            handler.send_header("X-Content-Type-Options", "nosniff")
            handler.send_header("Content-Security-Policy", "sandbox")
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.close_connection = True
            handler.wfile.write(first)
            while True:
                r, _, _ = select.select([fd, sock], [], [], 1.0)
                if sock in r and _client_closed(sock):
                    break
                if fd in r:
                    chunk = os.read(fd, 262144)
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
                elif proc.poll() is not None:
                    break
        except (BrokenPipeError, ConnectionError, TimeoutError, OSError):
            pass
        finally:
            _kill(proc)
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                pass
            _unregister(proc)
        return idx
    if errors:
        print(f"heliograph: playback stream failed {label}: " + " ;; ".join(errors), flush=True)
    return -1


__all__ = ["probe", "decide", "candidates", "stream_to", "stop_all_streams", "hardware", "describe"]
