"""Which files count as media, and what a Chromium window can show on its own.

The extension lists are deliberately broad (camera RAW, professional video, tracker-free audio,
legacy codecs). Whether a given *file* plays or decodes is decided later, per file, from its real
codecs (see playback.py) - the extension only decides what gets indexed.
"""

import mimetypes
import shutil
import subprocess

IMAGE_EXT = frozenset("""
jpg jpeg jpe jfif jfi jif png apng gif webp avif avifs heic heif heics heifs hif avci
bmp dib tif tiff ico cur svg svgz jxl jp2 j2k j2c jpf jpx jpm mj2 jxr wdp hdp tga icb vda vst
pcx ppm pgm pbm pnm pam pfm xbm xpm sgi rgb rgba bw dds psd psb xcf ora kra exr pic pict pct
qoi wbmp fits fit fts djvu djv emf wmf eps dpx cin jps mpo pgf bpg flif insp
3fr ari arw bay braw cr2 cr3 crw dcr dcs dng drf eip erf fff iiq k25 kdc mdc mef mos mrw nef
nrw orf pef ptx pxn r3d raf raw rw2 rwl rwz sr2 srf srw x3f gpr
""".split())

RAW_EXT = frozenset("""
3fr ari arw bay braw cr2 cr3 crw dcr dcs dng drf eip erf fff iiq k25 kdc mdc mef mos mrw nef
nrw orf pef ptx pxn r3d raf raw rw2 rwl rwz sr2 srf srw x3f gpr
""".split())

VIDEO_EXT = frozenset("""
mp4 m4v mp4v mov qt mkv mk3d webm avi divx xvid wmv asf flv f4v f4p mpg mpeg mpe mpv m1v m2v
mts m2ts m2t ts tp trp vob 3gp 3gpp 3g2 3gp2 ogv ogm rm rmvb dv dif mxf gxf lxf nut nsv ivf
amv mjpeg mjpg 264 h264 265 h265 hevc av1 obu bik smk wtv dvr-ms tod mpg4 y4m vro evo m2p
""".split())

AUDIO_EXT = frozenset("""
mp3 mp2 mpa mp1 m4a m4b m4p m4r aac adts flac wav wave w64 bwf aif aiff aifc caf ogg oga ogx
opus spx wma ape wv wvc tta tak ofr ofs mka mpc mp+ mpp ac3 eac3 ec3 dts dtshd mlp thd truehd
amr awb 3ga au snd voc 8svx gsm ra weba oma aa3 at3 qcp dsf dff sph ircam sd2
""".split())

KINDS = ("image", "video", "audio")

KIND_OF = {}
for _e in AUDIO_EXT:
    KIND_OF[_e] = "audio"
for _e in VIDEO_EXT:
    KIND_OF[_e] = "video"
for _e in IMAGE_EXT:                                # ext listed twice (e.g. "ts"): images win only when unique
    KIND_OF[_e] = "image"
for _e in ("ts", "tp", "trp"):                      # MPEG transport streams are video, not images
    KIND_OF[_e] = "video"

# Containers whose header can be read in pure Python without starting ffprobe (see meta.py).
BMFF_EXT = frozenset("mp4 m4v mov qt 3gp 3gpp 3g2 3gp2 m4a m4b m4p m4r".split())

# What a Chromium based window can show/play without help. Video and audio are judged per file
# from their codecs (playback.py); these are the *image* formats and a few audio containers.
NATIVE_IMAGE = frozenset("jpg jpeg jpe jfif jfi jif png apng gif webp avif bmp ico svg".split())
PIL_EXT = frozenset("jpg jpeg jpe jfif jfi jif png apng gif webp bmp dib tif tiff ico ppm pgm pbm "
                    "pnm tga icb vda vst pcx sgi rgb rgba bw dds qoi".split())

# Folders that are never worth walking.
SKIP_DIRS = frozenset({
    "node_modules", "__pycache__", "$RECYCLE.BIN", "System Volume Information", "lost+found",
    ".git", ".svn", ".hg", "@eaDir", "#recycle", ".AppleDouble", ".Trash", ".thumbnails"})

_MIME_FIXES = {
    ".mkv": "video/x-matroska", ".mov": "video/quicktime", ".m4v": "video/mp4", ".avi": "video/x-msvideo",
    ".wmv": "video/x-ms-wmv", ".flv": "video/x-flv", ".3gp": "video/3gpp", ".ogv": "video/ogg",
    ".m2ts": "video/mp2t", ".mts": "video/mp2t", ".ts": "video/mp2t", ".webm": "video/webm",
    ".mp4": "video/mp4", ".mp3": "audio/mpeg", ".flac": "audio/flac", ".m4a": "audio/mp4",
    ".opus": "audio/ogg", ".oga": "audio/ogg", ".ogg": "audio/ogg", ".wav": "audio/wav",
    ".aac": "audio/aac", ".avif": "image/avif", ".heic": "image/heic", ".heif": "image/heif",
    ".jfif": "image/jpeg", ".svg": "image/svg+xml", ".webp": "image/webp", ".jxl": "image/jxl",
    ".weba": "audio/webm", ".mka": "audio/x-matroska", ".apng": "image/apng"}
for _ext, _mime in _MIME_FIXES.items():
    mimetypes.add_type(_mime, _ext)


def ext_of(name):
    i = name.rfind(".")
    return name[i + 1:].lower() if i > 0 else ""


def kind_of(name):
    return KIND_OF.get(ext_of(name))


# Extensions that are also used by something that is not media. ".ts" is MPEG transport stream video, but also
# TypeScript source; a code folder must not fill a library with thousands of fake videos.
AMBIGUOUS_EXT = frozenset({"ts"})


def looks_like_media(ext, path):
    """True unless a content check says this file is not what its extension suggests (one tiny read, only for
    the few ambiguous extensions). MPEG-TS is made of 188 byte packets that each start with the byte 0x47."""
    if ext not in AMBIGUOUS_EXT:
        return True
    try:
        with open(path, "rb") as f:
            head = f.read(377)
    except OSError:
        return False
    return len(head) == 377 and head[0] == 0x47 and head[188] == 0x47 and head[376] == 0x47


def mime_of(name):
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def mime_types_for_desktop_file():
    """MIME types listed in the .desktop file so Heliograph appears in 'Open with' and can be
    chosen as the default player/viewer."""
    return [
        "inode/directory",
        "image/jpeg", "image/png", "image/gif", "image/webp", "image/avif", "image/bmp", "image/tiff",
        "image/heic", "image/heif", "image/jxl", "image/svg+xml", "image/x-icon", "image/x-tga",
        "image/jp2", "image/x-exr", "image/vnd.adobe.photoshop", "image/x-canon-cr2", "image/x-canon-cr3",
        "image/x-nikon-nef", "image/x-sony-arw", "image/x-adobe-dng", "image/x-olympus-orf",
        "image/x-fuji-raf", "image/x-panasonic-rw2", "image/x-pentax-pef",
        "video/mp4", "video/quicktime", "video/x-matroska", "video/webm", "video/x-msvideo",
        "video/x-ms-wmv", "video/x-flv", "video/mpeg", "video/mp2t", "video/3gpp", "video/3gpp2",
        "video/ogg", "video/x-ogm+ogg", "video/x-m4v", "video/dv", "video/vnd.rn-realvideo",
        "application/mxf",
        "audio/mpeg", "audio/flac", "audio/x-flac", "audio/mp4", "audio/x-m4a", "audio/aac",
        "audio/ogg", "audio/x-vorbis+ogg", "audio/opus", "audio/x-opus+ogg", "audio/wav", "audio/x-wav",
        "audio/webm", "audio/x-matroska", "audio/x-ms-wma", "audio/x-aiff", "audio/x-ape",
        "audio/x-wavpack", "audio/ac3", "audio/amr", "audio/basic", "audio/x-musepack"]


def capabilities():
    """What this machine can decode/encode (for `heliograph formats` and the About panel)."""
    caps = {"ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe")),
            "magick": bool(shutil.which("magick") or shutil.which("convert")),
            "mpv": bool(shutil.which("mpv")), "heif_convert": bool(shutil.which("heif-convert"))}
    try:
        import PIL                                   # noqa: F401
        caps["pillow"] = True
    except ImportError:
        caps["pillow"] = False
    return caps


def ffmpeg_lines(*args):
    """Run `ffmpeg -hide_banner <args>` and return the output lines ([] if ffmpeg is missing)."""
    exe = shutil.which("ffmpeg")
    if not exe:
        return []
    try:
        p = subprocess.run([exe, "-hide_banner", *args], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return p.stdout.splitlines()
