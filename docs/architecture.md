# How Heliograph works

Heliograph is two programs that talk over a local socket.

```
┌──────────────────────────── your desktop ────────────────────────────┐
│                                                                       │
│   Window                         Service (Python, stdlib only)        │
│   Chromium-based browser         ┌─────────────────────────────────┐  │
│   in app mode                    │ HTTP on 127.0.0.1               │  │
│   ┌──────────────────┐  fetch    │  /api/*  token + Host checks    │  │
│   │ virtual grid     │◄─────────►│  /thumb /preview /media /stream │  │
│   │ viewer + player  │           ├─────────────────────────────────┤  │
│   │ (ES modules,     │           │ views     snapshot ids, paging  │  │
│   │  no framework)   │           │ indexer   scan + enrich         │  │
│   └──────────────────┘           │ governor  CPU/IO/RAM/battery    │  │
│                                  │ thumbs    thumbs.db + previews  │  │
│   Terminal                       │ playback  direct/remux/transcode│  │
│   heliograph search|convert|…    │ convert   FFmpeg job queue      │  │
│   heliograph mcp  ◄── agents     └───────────────┬─────────────────┘  │
│                                      heliograph.db (SQLite, WAL)      │
└───────────────────────────────────────────────────────────────────────┘
```

The service starts on demand, exits by itself after 15 idle minutes (configurable), and is also what the command line and the MCP server talk to when they need something done (conversion, opening a folder). Searching the library from the terminal or MCP reads the database directly, read-only, so it works even when the service is not running.

## The rule: work only for what is on screen

Every design decision follows from one rule, **do the least work that shows the user what they asked for, and do it when the machine can afford it.**

1. **Scanning stores only name, size and date.** `os.scandir` is cheap, so a folder of 14,000 files is usable in seconds. Nothing is opened.
2. **Details are read in priority order.** Files on screen first (the window reports them), then everything that is cheap to read (picture headers, MP4/MOV headers parsed in pure Python), then anything that needs `ffprobe`, and only while the machine is idle.
3. **Views are snapshots of ids.** A "view" (a folder, a type, a search, a month) is sorted and grouped once and kept as an array of 8-byte integers: a million files is 8 MB. Any scroll position is then a slice plus a primary-key lookup of about 120 rows. There is no `OFFSET` scanning, so position 900,000 costs the same as position 0.
4. **The grid is virtual.** The page holds only the tiles that are visible plus a small margin, recycled as you scroll. A 14,000-file folder is about 35 DOM nodes.
5. **Thumbnails are requested for visible tiles only**, nearest to the centre first, four at a time, and cancelled when a tile leaves the screen. A fast fling (scrolling faster than tiles can load) requests nothing until it settles.
6. **The governor decides when background work may run.** It reads `/proc/stat`, the kernel's pressure-stall files, `MemAvailable` and the battery state. Busy machine: background work pauses. Idle machine: short bursts with a duty cycle. User waiting: always allowed, but limited to one to three parallel jobs.

## Storage

| What | Where | Rebuildable? |
| --- | --- | --- |
| Library folders, favourites, settings | `heliograph.db` + `user-data.json` snapshot | Restored from the snapshot if the database is damaged |
| File index, folder and month counts, stream probes, job history | `heliograph.db` | Yes, by rescanning |
| Thumbnails (400 px JPEG blobs, negative cache, LRU, 2048 MB default) | `thumbs.db` in the cache folder | Yes, safe to delete |
| Large previews (2400 px) | files in the cache folder, LRU pruned | Yes |

Locations follow the XDG layout (`~/.local/share/heliograph`, `~/.cache/heliograph`, `~/.local/state/heliograph`). Set `HELIOGRAPH_HOME` to put everything under one folder (used by the tests). The database uses WAL, `synchronous=NORMAL`, per-thread connections, versioned migrations (`PRAGMA user_version`), and covering indexes that match the two default sort orders so that building a view over a million rows does not sort.

## Playing anything

For every video or audio file the real streams are probed once and cached. The cheapest way to show the file is chosen:

| Mode | When | Cost |
| --- | --- | --- |
| **direct** | H.264 8-bit, VP8, VP9, AV1 or Theora in MP4/MOV/WebM/MKV/Ogg, with AAC, MP3, Opus, Vorbis or FLAC | The browser plays the original with range requests. No CPU |
| **remux** | Codecs are fine but the container is not (AVI, TS, FLV, MKV with AC-3, ...) | Video is copied, audio re-encoded only if needed, sent as fragmented MP4. About 1% CPU |
| **transcode** | HEVC, ProRes, MPEG-2, WMV, DivX, 10-bit H.264, HDR | Re-encoded to H.264/AAC on the GPU (VAAPI) when available, else software. HDR is tone-mapped. At most two streams at once |

If one way fails (for example the GPU is busy), the player falls through to the next. Seeking in a remuxed or transcoded stream restarts it at a keyframe at or before the target. When the viewer closes or the connection drops, the service stops FFmpeg.

## Security model

- Listens on `127.0.0.1`. The `Host` header must match, which blocks DNS rebinding. Cross-site browser requests (`Sec-Fetch-Site`) are refused.
- Every `/api/*` call except `/api/ping` needs a per-session token, which only the served page and the local command line know.
- Files are addressed by database id; the service serves a file only if it is in an opened folder, and system folders (`/`, `/etc`, `/proc`, ...) cannot be opened at all.
- Served files carry `Content-Security-Policy: sandbox` and `nosniff`, so an SVG or HTML file cannot run script. The page itself has a strict CSP and the interface never assigns `innerHTML`.
- FFmpeg and ImageMagick receive argument lists, never shell strings, and absolute paths.
- Conversion offers a fixed list of presets; free-form encoder arguments are not accepted.

Read [SECURITY.md](../SECURITY.md) for how to report a problem and [PRIVACY.md](../PRIVACY.md) for what is stored.

## Where things are in the code

See [ai/ARCHITECTURE.md](ai/ARCHITECTURE.md) for the file-by-file map, the invariants to keep, and the test layout.
