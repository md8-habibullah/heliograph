"""The `heliograph` command."""

import argparse
import json
import os
import shutil
import signal
import sys
import time
import urllib.parse
from pathlib import Path

from . import APP_ID, APP_NAME, __version__
from .formats import AUDIO_EXT, IMAGE_EXT, KIND_OF, VIDEO_EXT, capabilities, ext_of, ffmpeg_lines
from .paths import paths
from .util import fmt_duration, human

COMMANDS = {}


def command(name):
    def deco(fn):
        COMMANDS[name] = fn
        return fn
    return deco


def die(msg, code=1):
    print(f"heliograph: {msg}", file=sys.stderr)
    sys.exit(code)


# --------------------------------------------------------------------------- open

def cmd_open(argv):
    from . import client, window
    ap = argparse.ArgumentParser(
        prog="heliograph",
        description="Open Heliograph. Give it a folder to browse everything inside it, or a picture, video or "
                    "audio file to view it (the files next to it are one arrow-key away).",
        epilog="other commands: " + " | ".join(sorted(COMMANDS)) + "   (heliograph COMMAND --help)")
    ap.add_argument("paths", nargs="*", metavar="PATH", help="folders and/or media files")
    ap.add_argument("--add", action="store_true", help="add the folders to your library (default: just open them)")
    ap.add_argument("--tab", action="store_true", help="open in the default browser instead of an app window")
    ap.add_argument("--no-window", action="store_true", help="only start the service and index the folders")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args = ap.parse_intermixed_args(argv)
    try:
        info = client.ensure_server()
    except RuntimeError as e:
        die(str(e))
    focus_dir, focus_file = "", None
    for raw in args.paths:
        p = os.path.abspath(os.path.expanduser(raw))
        if not os.path.exists(p):
            die(f"{raw}: no such file or folder")
        try:
            res = client.call_api(info, "POST", "/api/open", {"path": p, "add": args.add})
        except RuntimeError as e:
            die(f"{raw}: {e}")
        focus_dir = res["dir"]
        focus_file = res.get("id")
    if args.no_window:
        print(f"service running on port {info['port']}")
        return 0
    base = f"http://127.0.0.1:{info['port']}/"
    st = client.call_api(info, "GET", "/api/status")
    if st["ui_alive"] and not args.tab:
        client.call_api(info, "POST", "/api/navigate", {"dir": focus_dir, "file": focus_file})
        for _ in range(16):                           # wait until the open window picked it up
            time.sleep(0.25)
            if not client.call_api(info, "GET", "/api/status")["nav_pending"]:
                window.focus_window()
                return 0
        client.call_api(info, "POST", "/api/navigate", {"dir": ""})
    h = {}
    if focus_dir:
        h["dir"] = focus_dir
    if focus_file:
        h["open"] = focus_file
    url = base + ("#" + urllib.parse.urlencode(h) if h else "")
    if not window.open_window(url, args.tab):
        die(f"could not open a window - open {url} in a browser")
    return 0


# --------------------------------------------------------------------------- service control

@command("serve")
def cmd_serve(argv):
    from .service import PREFERRED_PORT, serve
    ap = argparse.ArgumentParser(prog="heliograph serve", description="Run the service in the foreground.")
    ap.add_argument("--port", type=int, default=PREFERRED_PORT)
    ap.add_argument("--idle-exit", type=int, default=None, help="seconds without activity before quitting")
    a = ap.parse_args(argv)
    return serve(a.port, a.idle_exit)


@command("stop")
def cmd_stop(argv):
    from .client import pid_alive
    try:
        info = json.loads(paths().info.read_text())
    except (OSError, ValueError):
        print("not running")
        return 0
    pid = info.get("pid")
    if not pid_alive(pid):
        print("not running")
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(100):
        if not pid_alive(pid):
            print("stopped")
            return 0
        time.sleep(0.1)
    die("the service did not stop; kill it with: kill " + str(pid))


@command("monitor")
def cmd_monitor(argv):
    from . import monitor
    ap = argparse.ArgumentParser(prog="heliograph monitor", description="Live terminal status of the service.")
    ap.add_argument("--once", action="store_true", help="print one frame and exit")
    ap.add_argument("--interval", type=float, default=1.0)
    a = ap.parse_args(argv)
    return monitor.run(a.once, a.interval)


@command("status")
def cmd_status(argv):
    from . import client
    ap = argparse.ArgumentParser(prog="heliograph status", description="Service status (machine-readable with --json).")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.json:
        from . import monitor
        return monitor.run(True)
    info = client.server_info()
    if not info:
        print(json.dumps({"running": False}))
        return 1
    st = client.call_api(info, "GET", "/api/status")
    st["running"] = True
    print(json.dumps(st, indent=1))
    return 0


@command("rescan")
def cmd_rescan(argv):
    from . import client
    info = client.server_info() or die("the service is not running (start it with: heliograph)")
    client.call_api(info, "POST", "/api/rescan", {})
    print("rescan started - watch it with: heliograph monitor")
    return 0


@command("cache")
def cmd_cache(argv):
    ap = argparse.ArgumentParser(prog="heliograph cache", description="Thumbnail cache usage, pruning and clearing.")
    ap.add_argument("action", nargs="?", choices=("size", "prune", "clear"), default="size")
    a = ap.parse_args(argv)
    os.umask(0o077)
    from . import db, thumbs
    paths().ensure()
    if a.action == "clear":
        n = thumbs.clear()
        print(f"cleared {n:,} cached thumbnails; they are rebuilt when you scroll to them")
    elif a.action == "prune":
        print(f"removed {thumbs.prune():,} least recently used thumbnails")
    s = thumbs.cache_summary()
    print(f"{s['thumbs']:,} thumbnails, {human(s['thumbs_bytes'])} in {paths().thumbs_db}; "
          f"previews {human(s['previews_bytes'])} in {paths().previews}")
    return 0


@command("config")
def cmd_config(argv):
    ap = argparse.ArgumentParser(
        prog="heliograph config",
        description="Show or change settings. Changes apply at once (a running service reads them when it needs them).",
        epilog="examples: heliograph config | heliograph config thumb_cache_mb 1024 | heliograph config bg_index paused")
    ap.add_argument("key", nargs="?")
    ap.add_argument("value", nargs="?")
    ap.add_argument("--json", action="store_true", help="print as JSON")
    a = ap.parse_args(argv)
    os.umask(0o077)
    from . import db
    paths().ensure()
    db.init()
    if a.key and a.key not in db.SETTINGS:
        die(f"unknown setting {a.key!r}; known: {', '.join(db.SETTINGS)}")
    if a.key and a.value is not None:
        try:
            db.set_setting(a.key, db.validate_setting(a.key, a.value))
        except ValueError as e:
            die(str(e))
        db.write_user_data()                                   # the debounced snapshot would not fire before we exit
    cur = db.all_settings()
    keys = [a.key] if a.key else list(db.SETTINGS)
    if a.json:
        print(json.dumps({k: cur[k] for k in keys}, indent=1))
    elif a.key and a.value is None:
        print(cur[a.key])
    else:
        for k in keys:
            d, allowed, what = db.SETTINGS[k]
            mark = "" if cur[k] == d else "  (changed)"
            print(f"{k} = {cur[k]}{mark}\n    {what}\n    choices: {', '.join(allowed)}  default: {d}")
    return 0


# --------------------------------------------------------------------------- conversion without the window

SHORTCUTS = {
    "jpg": "img-jpg", "jpeg": "img-jpg", "png": "img-png", "avif": "img-avif", "jxl": "img-jxl", "tiff": "img-tiff",
    "tif": "img-tiff", "bmp": "img-bmp", "webp": "img-webp",
    "mp4": "vid-mp4-h264", "h264": "vid-mp4-h264", "h265": "vid-mp4-hevc", "hevc": "vid-mp4-hevc",
    "webm": "vid-webm-vp9", "vp9": "vid-webm-vp9", "av1": "vid-webm-av1", "mkv": "vid-mkv-copy",
    "mp4-copy": "vid-mp4-copy", "mov": "vid-mov-prores", "prores": "vid-mov-prores", "gif": "vid-gif",
    "mp3": "aud-mp3", "m4a": "aud-m4a", "aac": "aud-m4a", "opus": "aud-opus", "ogg": "aud-ogg",
    "flac": "aud-flac", "wav": "aud-wav"}


@command("convert")
def cmd_convert(argv):
    from . import convert
    ap = argparse.ArgumentParser(
        prog="heliograph convert",
        description="Convert pictures, videos and audio with FFmpeg. Originals are never changed or overwritten.")
    ap.add_argument("inputs", nargs="*", metavar="FILE")
    ap.add_argument("--to", metavar="FORMAT", help="target: a preset id (see --list) or a short name like mp4, webm, "
                    "jpg, avif, mp3, flac, gif")
    ap.add_argument("--quality", choices=convert.QUALITIES, default="balanced")
    ap.add_argument("--max-height", type=int, default=0, help="video: scale down to this height (e.g. 1080)")
    ap.add_argument("--max-size", type=int, default=0, help="picture: longest side in pixels")
    ap.add_argument("--out", metavar="DIR", help="output folder (default: next to the original)")
    ap.add_argument("--gpu", choices=("auto", "off"), default="auto", help="use VAAPI hardware encoding when possible")
    ap.add_argument("--strip", action="store_true", help="drop metadata (EXIF, tags)")
    ap.add_argument("--list", action="store_true", help="list presets and exit")
    ap.add_argument("--json", action="store_true", help="print one JSON line per result")
    a = ap.parse_args(argv)
    if a.list:
        for kind in ("image", "video", "audio"):
            print(f"{kind}:")
            for pr in convert.presets_for(kind):
                print(f"  {pr['id']:16} .{pr['ext']:5} {pr['label']} - {pr['desc']}")
        return 0
    if not a.inputs or not a.to:
        ap.error("give at least one FILE and --to FORMAT (or --list)")
    status = 0
    for raw in a.inputs:
        src = os.path.abspath(os.path.expanduser(raw))
        kind = KIND_OF.get(ext_of(src))
        if not os.path.isfile(src) or not kind:
            print(f"skipped {raw}: not a picture, video or audio file", file=sys.stderr)
            status = 1
            continue
        pid = a.to if a.to in convert.PRESETS else None
        if pid is None:
            short = SHORTCUTS.get(a.to.lower())
            if a.to.lower() in ("webp", "gif") and kind == "video":
                short = "vid-webp" if a.to.lower() == "webp" else "vid-gif"
            if a.to.lower() in ("mp3", "m4a", "aac", "opus", "ogg", "flac", "wav", "mka"):
                short = SHORTCUTS.get(a.to.lower())
            pid = short
        if not pid or kind not in convert.PRESETS[pid][2]:
            print(f"skipped {raw}: cannot make '{a.to}' from a {kind} file (see --list)", file=sys.stderr)
            status = 1
            continue
        spec = {"src": src, "kind": kind, "preset": pid, "quality": a.quality, "max_height": a.max_height,
                "max_size": a.max_size, "gpu": a.gpu, "strip": a.strip, "out_dir": a.out}
        try:
            convert.validate_spec(spec, kind)
        except Exception as e:
            print(f"skipped {raw}: {getattr(e, 'message', e)}", file=sys.stderr)
            status = 1
            continue

        def progress(f, name=os.path.basename(src)):
            if sys.stderr.isatty():
                w = 28
                n = int(f * w)
                sys.stderr.write(f"\r{name[:34]:34} {'█' * n}{'░' * (w - n)} {f * 100:3.0f}%")
                sys.stderr.flush()

        t0 = time.time()
        try:
            res = convert.run_spec(spec, progress)
        except Exception as e:
            if sys.stderr.isatty():
                sys.stderr.write("\n")
            print(f"failed {raw}: {e}", file=sys.stderr)
            status = 1
            continue
        if sys.stderr.isatty():
            sys.stderr.write("\n")
        if a.json:
            print(json.dumps({"src": src, "dst": res["dst"], "bytes_in": res["bytes_in"], "bytes_out": res["bytes_out"],
                              "seconds": round(time.time() - t0, 2), "preset": pid}))
        else:
            print(f"{res['dst']}  ({human(res['bytes_in'])} -> {human(res['bytes_out'])}, "
                  f"{fmt_duration(time.time() - t0)})")
    return status


# --------------------------------------------------------------------------- inspection (no window, agent friendly)

@command("search")
def cmd_search(argv):
    from . import db, views
    ap = argparse.ArgumentParser(prog="heliograph search",
                                 description="Search the library from the terminal (read-only; no service needed).")
    ap.add_argument("query", nargs="*", help="words that must all appear in the name, folder, title or artist")
    ap.add_argument("--kind", choices=("image", "video", "audio"))
    ap.add_argument("--in", dest="folder", help="only below this folder")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not paths().db.exists():
        die("there is no library yet - open a folder with: heliograph PATH --add")
    p = {"q": " ".join(a.query), "kind": a.kind or "", "dir": os.path.abspath(a.folder) if a.folder else ""}
    where, args = views.where_from(p)
    c = db.connect(readonly=True)
    try:
        rows = c.execute("SELECT path,kind,size,ts,w,h,dur,title,artist FROM files" + where +
                         " ORDER BY ts DESC LIMIT ?", args + [max(1, min(a.limit, 5000))]).fetchall()
    finally:
        c.close()
    for r in rows:
        d = {k: r[k] for k in r.keys() if r[k] is not None}
        if a.json:
            print(json.dumps(d, ensure_ascii=False))
        else:
            dim = f" {r['w']}x{r['h']}" if r["w"] else ""
            dur = f" {fmt_duration(r['dur'])}" if r["dur"] else ""
            print(f"{r['kind']:5} {human(r['size']):>9}{dim}{dur}  {r['path']}")
    return 0 if rows else 1


@command("info")
def cmd_info(argv):
    from . import meta, playback
    ap = argparse.ArgumentParser(prog="heliograph info", description="Show what is inside a media file and how it plays.")
    ap.add_argument("file")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    src = os.path.abspath(os.path.expanduser(a.file))
    kind = KIND_OF.get(ext_of(src))
    if not os.path.isfile(src) or not kind:
        die("not a picture, video or audio file")
    out = {"path": src, "kind": kind, "size": os.path.getsize(src)}
    if kind == "image":
        try:
            out.update(meta.probe_image(src))
        except Exception:
            out.update(meta.probe_ffprobe(src))
    else:
        j = meta.ffprobe_json(src)
        if not j:
            die("ffprobe could not read the file")
        info = playback.describe(j, src)
        out["info"] = info
        out["play"] = playback.decide(info, kind)
    if a.json:
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return 0
    print(f"{src}\n  {kind}, {human(out['size'])}")
    if kind == "image":
        print(f"  {out.get('w')}x{out.get('h')} px")
    else:
        i = out["info"]
        v = i.get("video")
        if v:
            print(f"  video: {v['codec']} {v.get('profile') or ''} {v['w']}x{v['h']} {v.get('pix_fmt')} {v.get('fps') or '?'} fps")
        for s in i.get("audio", []):
            print(f"  audio: {s['codec']} {s.get('channels')}ch {s.get('lang') or ''} {s.get('title') or ''}")
        for s in i.get("subs", []):
            print(f"  subtitles: {s['codec']} {s.get('lang') or ''} {s.get('title') or ''}")
        print(f"  duration: {fmt_duration(i['dur']) if i.get('dur') else '?'}   container: {i['container']}")
        print(f"  playback: {out['play']['mode']} - {out['play']['why']}")
    return 0


@command("formats")
def cmd_formats(argv):
    from . import playback
    ap = argparse.ArgumentParser(prog="heliograph formats", description="What Heliograph indexes and what this machine can decode.")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    dec = ffmpeg_lines("-decoders")
    enc = ffmpeg_lines("-encoders")
    n_dec = sum(1 for ln in dec if ln.startswith(" V") or ln.startswith(" A"))
    n_enc = sum(1 for ln in enc if ln.startswith(" V") or ln.startswith(" A"))
    n_mux = sum(1 for ln in ffmpeg_lines("-muxers") if ln.startswith("  E") or ln.startswith(" E"))
    n_dmx = sum(1 for ln in ffmpeg_lines("-demuxers") if ln.startswith("  D") or ln.startswith(" D"))
    hw = playback.hardware()
    data = {"indexed_extensions": {"image": len(IMAGE_EXT), "video": len(VIDEO_EXT), "audio": len(AUDIO_EXT)},
            "ffmpeg": {"decoders": n_dec, "encoders": n_enc, "demuxers": n_dmx, "muxers": n_mux},
            "tools": capabilities(), "vaapi": {"device": hw["device"], "decode": sorted(hw["decode"]),
                                               "encode_h264": hw["encode_h264"]},
            "image": sorted(IMAGE_EXT), "video": sorted(VIDEO_EXT), "audio": sorted(AUDIO_EXT)}
    if a.json:
        print(json.dumps(data, indent=1))
        return 0
    print(f"Indexed file types: {len(IMAGE_EXT)} picture, {len(VIDEO_EXT)} video, {len(AUDIO_EXT)} audio extensions")
    print(f"FFmpeg on this machine: {n_dec} audio/video decoders, {n_enc} encoders, {n_dmx} input and {n_mux} output formats")
    print("Tools: " + ", ".join(f"{k} {'yes' if v else 'no'}" for k, v in data["tools"].items()))
    print(f"GPU (VAAPI): {hw['device'] or 'not available'}" + (f" · decodes {', '.join(sorted(hw['decode']))}"
                                                              f" · H.264 encode {'yes' if hw['encode_h264'] else 'no'}" if hw["device"] else ""))
    for k in ("image", "video", "audio"):
        print(f"\n{k}: " + " ".join(data[k]))
    return 0


@command("doctor")
def cmd_doctor(argv):
    from . import client, db, install, playback, window
    ap = argparse.ArgumentParser(prog="heliograph doctor", description="Check that everything Heliograph needs works.")
    a = ap.parse_args(argv)
    bad = 0

    def line(ok, msg, hint=""):
        nonlocal bad
        if not ok:
            bad += 1
        print(f"  {'✓' if ok else '✗'} {msg}" + (f"\n      {hint}" if hint and not ok else ""))

    print(f"{APP_NAME} {__version__} on Python {sys.version.split()[0]}")
    line(sys.version_info >= (3, 10), "Python 3.10 or newer", "install a newer Python")
    caps = capabilities()
    line(caps["ffmpeg"] and caps["ffprobe"], "ffmpeg + ffprobe (playback fallback, conversion, video details)",
         "install ffmpeg (e.g. sudo pacman -S ffmpeg / sudo apt install ffmpeg)")
    line(caps["pillow"], "Pillow (fast picture thumbnails)", "install it: pip install pillow  (or your distro's python-pillow)")
    line(caps["magick"], "ImageMagick (HEIC / RAW / PSD pictures)", "optional: install imagemagick")
    line(caps["mpv"], "mpv (optional external player)", "optional: install mpv")
    b = window.find_browser()
    line(bool(b), f"Chromium-based browser for the app window ({b[0] if b else 'none found'})",
         "install Brave, Chromium or Chrome, or set HELIOGRAPH_BROWSER; without one the app opens in your default browser")
    hw = playback.hardware()
    print(f"  · GPU video acceleration (VAAPI): {'yes - ' + hw['device'] if hw['device'] and hw['encode_h264'] else 'no (software encoding is used)'}")
    p = paths()
    print(f"  · data   {p.data}\n  · cache  {p.cache}\n  · state  {p.state}")
    if p.db.exists():
        ok, msg = db.integrity_report(full=True)
        line(ok, f"database integrity ({msg})", "the service rebuilds a damaged database at next start; "
                                                  "your folders and favourites come from user-data.json")
        mode = oct(p.db.stat().st_mode & 0o777)
        line(mode == "0o600", f"database permissions are private ({mode})", f"fix with: chmod 600 {p.db}")
    else:
        print("  · no library database yet (it is created the first time you open a folder)")
    info = client.server_info()
    print(f"  · service: {'running on port ' + str(info['port']) if info else 'not running (starts when you open the app)'}")
    d = install.applications_dir() / install.DESKTOP_NAME
    line(d.exists(), "launcher installed (app menu / 'Open with')", "run: heliograph install")
    print("\n" + ("All good." if not bad else f"{bad} problem(s) found."))
    return 1 if bad else 0


@command("install")
def cmd_install(argv):
    from . import install
    ap = argparse.ArgumentParser(prog="heliograph install",
                                 description="Add Heliograph to the app menu and to 'Open with' (does not change defaults).")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--exec", help="path of the heliograph command to put in the launcher")
    ap.add_argument("--wm-class", help="window class / app id to match (default: worked out for your browser; "
                                       "see `hyprctl clients` or `xprop WM_CLASS` while the window is open)")
    a = ap.parse_args(argv)
    if a.uninstall:
        for f in install.uninstall():
            print("removed", f)
        return 0
    r = install.install(a.exec, a.wm_class)
    print(f"launcher  {r['desktop']}\nicon      {r['icon']}\ncommand   {r['exec']}\nwindow class  {r['wm_class']}")
    print("Heliograph now appears in your app menu and in 'Open with' for pictures, videos, audio and folders.\n"
          "To make it the default for some of them: heliograph default --images --videos --audio")
    return 0


@command("default")
def cmd_default(argv):
    from . import install
    ap = argparse.ArgumentParser(prog="heliograph default",
                                 description="Make Heliograph the default app for pictures / videos / audio / folders. "
                                             "The previous defaults are remembered so --restore can undo this.")
    for g in install.DEFAULT_GROUPS:
        ap.add_argument(f"--{g}", action="store_true", help=f"default for {g}")
    ap.add_argument("--restore", action="store_true", help="give the defaults back to the previous apps")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args(argv)
    if a.status:
        for g, m in install.default_status().items():
            print(g, m)
        return 0
    if a.restore:
        done = install.restore_defaults()
        print(f"restored {len(done)} file types" if done else "nothing to restore")
        return 0
    groups = [g for g in install.DEFAULT_GROUPS if getattr(a, g)]
    if not groups:
        ap.error("choose at least one of: " + " ".join("--" + g for g in install.DEFAULT_GROUPS))
    if not (install.applications_dir() / install.DESKTOP_NAME).exists():
        install.install()
    changed = install.set_defaults(groups)
    print(f"Heliograph is now the default for {len(changed)} file types ({', '.join(groups)}). Undo: heliograph default --restore")
    return 0


@command("mcp")
def cmd_mcp(argv):
    from . import mcp
    ap = argparse.ArgumentParser(prog="heliograph mcp",
                                 description="Run an MCP server on stdin/stdout so AI agents can search and inspect "
                                             "your library (read-only unless --allow-convert).")
    ap.add_argument("--allow-convert", action="store_true", help="also expose the convert_media tool")
    a = ap.parse_args(argv)
    return mcp.serve(allow_convert=a.allow_convert)


@command("version")
def cmd_version(argv):
    print(f"{APP_NAME} {__version__} ({APP_ID})")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in COMMANDS:
        return COMMANDS[argv[0]](argv[1:]) or 0
    if argv and argv[0] == "open" and not os.path.exists("open"):          # `heliograph open PATH` reads naturally
        argv = argv[1:]
    return cmd_open(argv)


__all__ = ["main", "shutil", "Path"]
