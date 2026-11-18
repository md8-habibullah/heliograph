"""Reading details (size in pixels, duration, taken date, tags) from media files - cheaply.

Three readers, from cheapest to most expensive:
  * probe_image  - Pillow reads only the header and EXIF; the picture is not decoded.
  * probe_bmff   - pure-Python reader for MP4 / MOV / M4A / 3GP: parses the `moov` box only
                   (seeks past the media data), no process is started.
  * probe_ffprobe - runs ffprobe; used for every other container (MKV, AVI, MP3, ...) and when
                   the full stream list is needed (playback decisions, details panel).
"""

import json
import os
import shutil
import struct

from .util import parse_dt, run_low

_MAC_EPOCH = 2082844800            # seconds between 1904-01-01 and 1970-01-01
_MAX_MOOV = 96 * 1024 * 1024

VIDEO_FOURCC = {
    b"avc1": "h264", b"avc3": "h264", b"hvc1": "hevc", b"hev1": "hevc", b"dvh1": "hevc", b"dvhe": "hevc",
    b"mp4v": "mpeg4", b"av01": "av1", b"vp09": "vp9", b"vp08": "vp8", b"jpeg": "mjpeg", b"mjpa": "mjpeg",
    b"apch": "prores", b"apcn": "prores", b"apcs": "prores", b"apco": "prores", b"ap4h": "prores",
    b"ap4x": "prores", b"s263": "h263", b"cvid": "cinepak", b"rle ": "qtrle", b"svq3": "svq3",
    b"dvc ": "dvvideo", b"dvcp": "dvvideo", b"dvh5": "dvvideo", b"xd5b": "mpeg2video", b"mx5n": "mpeg2video"}
AUDIO_FOURCC = {
    b"mp4a": "aac", b".mp3": "mp3", b"ac-3": "ac3", b"ec-3": "eac3", b"alac": "alac", b"Opus": "opus",
    b"fLaC": "flac", b"sowt": "pcm_s16le", b"twos": "pcm_s16be", b"in24": "pcm_s24le", b"lpcm": "pcm",
    b"raw ": "pcm_u8", b"samr": "amr_nb", b"sawb": "amr_wb", b"ulaw": "pcm_mulaw", b"alaw": "pcm_alaw"}


# --------------------------------------------------------------------------- images

def probe_image(path):
    from PIL import Image
    with Image.open(path) as im:
        w, h = im.size
        taken = None
        try:
            ex = im.getexif()
            if ex.get(0x0112) in (5, 6, 7, 8):
                w, h = h, w
            raw = None
            try:
                raw = ex.get_ifd(0x8769).get(0x9003)
            except Exception:
                pass
            taken = parse_dt(raw or ex.get(0x9003) or ex.get(0x0132))
        except Exception:
            pass
    return {"w": w, "h": h, "taken": taken}


# --------------------------------------------------------------------------- MP4 / MOV

def _boxes(data, start=0, end=None):
    """Yield (type, payload_start, payload_end) for the boxes in data[start:end]."""
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, typ = struct.unpack_from(">I4s", data, pos)
        hdr = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            return
        yield typ, pos + hdr, pos + size
        pos += size


def _find(data, start, end, *path):
    """Payload bounds of the first box found by following a path of box types."""
    for want in path:
        found = None
        for typ, a, b in _boxes(data, start, end):
            if typ == want:
                found = (a, b)
                break
        if not found:
            return None
        start, end = found
    return start, end


def _ilst_text(data, a, b):
    for typ, da, db_ in _boxes(data, a, b):
        if typ == b"data" and db_ - da > 8:
            try:
                return data[da + 8:db_].decode("utf-8", "replace").strip("\x00").strip() or None
            except Exception:
                return None
    return None


def _parse_moov(data):
    res = {}
    mv = _find(data, 0, len(data), b"mvhd")
    timescale = 0
    if mv:
        a, b = mv
        ver = data[a]
        if ver == 1 and b - a >= 32:
            created, _, timescale, dur = struct.unpack_from(">QQIQ", data, a + 4)
        elif b - a >= 20:
            created, _, timescale, dur = struct.unpack_from(">IIII", data, a + 4)
        else:
            created, timescale, dur = 0, 0, 0
        if timescale and dur and dur != 0xFFFFFFFF:
            res["dur"] = dur / timescale
        if created > _MAC_EPOCH:
            ts = created - _MAC_EPOCH
            if 631152000 <= ts <= 4102444800:             # 1990 .. 2100
                res["taken"] = ts
    for typ, ta, tb in _boxes(data, 0, len(data)):
        if typ != b"trak":
            continue
        hd = _find(data, ta, tb, b"mdia", b"hdlr")
        kind = data[hd[0] + 8:hd[0] + 12] if hd and hd[1] - hd[0] >= 12 else b""
        sd = _find(data, ta, tb, b"mdia", b"minf", b"stbl", b"stsd")
        fourcc = data[sd[0] + 12:sd[0] + 16] if sd and sd[1] - sd[0] >= 16 else b""
        if kind == b"vide" and "codec" not in res:
            tk = _find(data, ta, tb, b"tkhd")
            if tk:
                a, b = tk
                ver = data[a]
                off = 4 + (32 if ver == 1 else 20) + 8 + 8        # -> matrix
                if b - a >= off + 44:
                    m = struct.unpack_from(">9i", data, a + off)
                    w, h = struct.unpack_from(">II", data, a + off + 36)
                    w, h = w >> 16, h >> 16
                    if m[0] == 0 and m[4] == 0:                   # rotated 90 / 270 degrees
                        w, h = h, w
                    if w and h:
                        res["w"], res["h"] = w, h
            if fourcc:
                res["codec"] = VIDEO_FOURCC.get(fourcc, fourcc.decode("latin-1").strip().lower())
        elif kind == b"soun" and "acodec" not in res and fourcc:
            res["acodec"] = AUDIO_FOURCC.get(fourcc, fourcc.decode("latin-1").strip().lower())
    if "codec" not in res and "acodec" in res:
        res["codec"] = res["acodec"]
    res.pop("acodec", None)
    il = _find(data, 0, len(data), b"udta", b"meta")
    if il:                                                          # meta is a "full box": 4 extra bytes
        ilst = _find(data, il[0] + 4, il[1], b"ilst")
        if ilst:
            names = {b"\xa9nam": "title", b"\xa9ART": "artist", b"aART": "artist", b"\xa9alb": "album"}
            for typ, a, b in _boxes(data, ilst[0], ilst[1]):
                key = names.get(typ)
                if key and key not in res:
                    val = _ilst_text(data, a, b)
                    if val:
                        res[key] = val
    return res


def probe_bmff(path):
    """Duration, size in pixels, creation date, codec and tags of an MP4/MOV/M4A without ffprobe.
    Returns {} when the file is not a readable ISO-BMFF file (the caller then uses ffprobe)."""
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            pos = 0
            for _ in range(64):                                   # top-level boxes are few
                if pos + 8 > size:
                    return {}
                f.seek(pos)
                hdr = f.read(16)
                bsize, typ = struct.unpack_from(">I4s", hdr, 0)
                hl = 8
                if bsize == 1 and len(hdr) >= 16:
                    bsize, hl = struct.unpack_from(">Q", hdr, 8)[0], 16
                elif bsize == 0:
                    bsize = size - pos
                if bsize < hl:
                    return {}
                if typ == b"moov":
                    if bsize - hl > _MAX_MOOV:
                        return {}
                    f.seek(pos + hl)
                    return _parse_moov(f.read(bsize - hl))
                pos += bsize
    except (OSError, struct.error, IndexError, ValueError):
        return {}
    return {}


# --------------------------------------------------------------------------- ffprobe

def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def ffprobe_json(path, timeout=30):
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    rc, out = run_low([exe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", "--", path],
                      timeout)
    if rc != 0 or not out:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def summarize(j):
    """Normalize ffprobe's JSON into the fields the library stores."""
    fmt = j.get("format") or {}
    streams = j.get("streams") or []
    tags = {str(k).lower(): v for k, v in (fmt.get("tags") or {}).items()}
    vs = next((s for s in streams if s.get("codec_type") == "video"
               and not (s.get("disposition") or {}).get("attached_pic")), None)
    au = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if au:
        for k, v in (au.get("tags") or {}).items():
            tags.setdefault(str(k).lower(), v)
    res = {}
    d = _num(fmt.get("duration"))
    if d is not None and d >= 0:
        res["dur"] = d
    if vs:
        w, h = vs.get("width"), vs.get("height")
        rot = (vs.get("tags") or {}).get("rotate")
        for sd in vs.get("side_data_list") or []:
            rot = sd.get("rotation", rot)
        try:
            if rot is not None and abs(int(float(rot))) % 180 == 90:
                w, h = h, w
        except (TypeError, ValueError):
            pass
        res.update(w=w, h=h, codec=vs.get("codec_name"))
    elif au:
        res["codec"] = au.get("codec_name")
    res["title"] = tags.get("title")
    res["artist"] = tags.get("artist") or tags.get("album_artist")
    res["album"] = tags.get("album")
    res["taken"] = parse_dt(tags.get("creation_time"), utc=True)
    return res


def probe_ffprobe(path):
    j = ffprobe_json(path)
    return summarize(j) if j else {}


def probe_cheap(row_kind, ext, path):
    """Details from the cheapest reader that suits the file. {} means 'could not read'."""
    from .formats import BMFF_EXT, PIL_EXT, RAW_EXT
    if row_kind == "image":
        try:
            return probe_image(path)
        except Exception:
            if ext in PIL_EXT or ext in RAW_EXT:
                return {}                       # unreadable, or RAW: thumbnails still go through ImageMagick
            return probe_ffprobe(path)          # jxl / jp2 / exr / ...: ffprobe knows many more
    if ext in BMFF_EXT:
        r = probe_bmff(path)
        if r:
            return r
    return probe_ffprobe(path)


def cost_of(kind, ext):
    """1 when details come from a cheap in-process read, 2 when ffprobe is needed."""
    from .formats import BMFF_EXT, PIL_EXT
    if kind == "image":
        return 1 if ext in PIL_EXT else 2
    return 1 if ext in BMFF_EXT else 2
