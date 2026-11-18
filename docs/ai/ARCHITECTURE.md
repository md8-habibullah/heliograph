# Architecture map (for agents and new contributors)

Read [AGENTS.md](../../AGENTS.md) for the rules and [../architecture.md](../architecture.md) for the narrative. This file answers "where is X and what must I not break".

## Layout

```
src/heliograph/
  cli.py         the `heliograph` command and its sub-commands (open, search, info, convert, config, cache, ...)
  client.py      CLI side of talking to the service: find it, start it, call its API with the session token
  service.py     starts HTTP server + Indexer + JobManager; writes server.json (pid/port/token); idle exit
  server.py      HTTP handler: guards, JSON API, /media /thumb /preview /stream /sub routes
  views.py       snapshot views (id arrays), paging, folders, timeline, folder picker, roots, favourites
  indexer.py     scan (name/size/date only) and enrich (details) threads; want() for on-screen files
  meta.py        cheap detail readers: Pillow header, pure-Python MP4/MOV header, ffprobe
  thumbs.py      thumbnail + preview generation and the thumbs.db blob cache (LRU)
  playback.py    ffprobe -> describe -> decide (direct/remux/transcode) -> ffmpeg command candidates
  convert.py     presets, validation, ffmpeg runners, JobManager (one job at a time)
  governor.py    CPU / pressure-stall / memory / battery -> idle|normal|busy|paused
  stats.py       per-folder and per-month aggregate tables (so the sidebar never scans `files`)
  db.py          SQLite schema + migrations, connections, settings table, user-data snapshot, recovery
  formats.py     extension tables, what a browser shows natively, ambiguous-extension check
  mcp.py         stdio MCP server (read-only by default)
  install.py     .desktop file, icon, default-application handling (xdg-mime)
  window.py      launches the Chromium-based browser in app mode
  monitor.py     `heliograph monitor` terminal status
  state.py       process-wide runtime state (versions, status dicts, GOVERNOR)
  util.py        natural sort keys, low-priority subprocess helpers, ApiError, to_int, path helpers
  paths.py       XDG locations, HELIOGRAPH_HOME override
  web/           index.html, style.css, core.js, grid.js, nav.js, player.js, dialogs.js, help.js, main.js, icon.svg
tests/           unittest suite (helpers.py: IsolatedHome base class)
tools/           synth_library.py (scale data), scale_probe.py (timings)
packaging/       PKGBUILD, AppStream metainfo, desktop file
```

## Data model (SQLite, `heliograph.db`, schema version 1)

| Table | Purpose |
| --- | --- |
| `roots` | Folders being indexed. `persist=1` is the library, `persist=0` is "opened for now" (removed when the service starts) |
| `files` | One row per media file: path, dir, name, ext, kind, size, mtime, ts (display date), w/h/dur, tags, `search` (lower-cased name+folder+tags, matched with `instr`), `lib`, `seen` (scan generation), `meta` (details read?), `cost` (1 cheap read, 2 needs ffprobe) |
| `dirstats`, `monthstats` | Aggregates rebuilt by `stats.refresh()` after a scan |
| `favourites` | Keyed by **path**, so they survive rescans |
| `settings` | `db.SETTINGS` is the schema of valid keys |
| `probes` | Cached `playback.describe()` result per file (invalidated by mtime/size and `DESCRIBE_VERSION`) |
| `jobs` | Conversion history |

Covering indexes `files_ord_folder` and `files_ord_date` match the two default sort orders; changing the default order means changing them (new migration). `thumbs.db` (cache folder) is separate and disposable.

## Request flow: showing a screen

1. UI `GET /api/view?...` -> `views.api_view`: builds (or reuses) a **snapshot**: `array('q')` of ids sorted and grouped once, plus a group list. Keyed by params + library version, 6 kept (LRU).
2. UI `GET /api/items?view=ID&offset&limit` -> slice of ids -> `SELECT ... WHERE id IN (...)`. Constant time at any offset. An expired view returns 410 and the UI rebuilds it.
3. For each visible tile the UI `GET /thumb/<id>?k=<tag>` -> `thumbs.get_thumb` (cache hit, or generate under the governor's worker limit, or a remembered failure).
4. Tiles whose details are missing are reported with `POST /api/want`; `Indexer.want_ids` reads them first.
5. A fling (scrolling faster than tiles can load) requests nothing until it settles (`grid.js`: `fast`, `settle()`).

## Request flow: playing a video

`/api/item/<id>` returns the `describe()` summary and `decide()` verdict. The player picks `/media/<id>` (direct) or `/stream/<id>?mode=remux|transcode&start=T&audio=N`. `server._stream` runs the first working command from `playback.candidates()` (VAAPI, then software), pipes fragmented MP4 to the client, stops FFmpeg when the connection closes, and allows at most two streams. `/api/keyframe` snaps seeks in remuxed streams to a keyframe.

## Invariants (tests enforce most of them)

- Every `/api/*` route except `/api/ping` needs `X-Heliograph-Token`; `Host` must be local; cross-site requests are refused (`tests/test_server.py`).
- Media routes take an integer id (`\d{1,18}`); a path never comes from the request. Ids and numeric parameters go through `util.to_int` (clean 400, never a 500).
- A `.ts` file is indexed only if it looks like MPEG-TS (`formats.looks_like_media`).
- Scans use strictly increasing generations (`indexer.next_gen`); a file is deleted only when not seen in the newest scan and not inside an unreadable folder.
- Search is `instr(search, ?)`: wildcards are literal.
- `convert.validate_spec` is the only gate in front of FFmpeg options; stream-copy presets reject a resize.
- Each HTTP connection thread closes its SQLite handles when it ends (`Handler.handle`).
- The web interface never uses `innerHTML` (`tests/test_web.py`).
- **File ids are not forever.** `files.id` has no AUTOINCREMENT, and removing a folder (or the service restarting, for "opened for now" folders) frees its ids for other files. So nothing in the page may cache or request a file by id alone: every item, cover and detail record carries `k` (`util.file_tag`: a hash of path, size and modified time), the page keys its thumbnail cache by `id:k`, builds URLs with `fileUrl()` (`core.js`), and the server answers 404 (never another file's picture) when `?k=` does not match the file now behind the id. `tests/test_server.py` (id-reuse scenario) and `tests/test_web.py` guard this.
- The only addresses in the interface are the developer's site and the repository (`__init__.AUTHOR_SITE`, `REPO_URL`), shown as links in the About tab; `tests/test_web.py` fails if another one appears.

## Recipes

**Add a file format.** Add the extension to `formats.py` (+ `RAW_EXT`/`PIL_EXT`/`BMFF_EXT` if a special reader applies). If it needs a decoder, extend the chain in `thumbs.generate`. If the extension is ambiguous with non-media (like `ts`), add it to `AMBIGUOUS_EXT` and a content check. Update `docs/formats.md`. `tests/test_formats.py` guards the breadth.

**Add a conversion preset.** Add to `convert.PRESETS` (id, label, description, input kinds, output kind, extension, muxer) and a command branch in `_image_cmds`/`_video_cmds`/`_audio_cmds`. `tests/test_convert.py::EveryPresetTest` runs every preset on a tiny real input, so yours is covered automatically. Update `docs/conversion.md` and `docs/mcp.md` counts.

**Add a setting.** Add it to `db.SETTINGS` (default, allowed values, description) and read it with `db.get_setting`. The API, `heliograph config` and validation pick it up. Document it in `docs/performance.md`.

**Add an API route.** Add it in `server.py` (`_get` or `_post`), validate every parameter (`to_int`, `ApiError`), and add a test in `tests/test_server.py` including a malformed-input case.

**Change the schema.** Append a migration to `db.MIGRATIONS`, bump `SCHEMA_VERSION`, keep the snapshot in step (`write_user_data` / `restore_user_data`), add a test in `tests/test_db.py`.

**Change what `describe()` returns.** Bump `playback.DESCRIBE_VERSION`.

**Add an MCP tool.** Add its schema to `mcp.TOOLS`, a method on `mcp.Tools`, and tests in `tests/test_mcp.py` (including a path-escape case). Anything that writes belongs behind `--allow-convert`.

## Testing notes

- `tests/helpers.py: IsolatedHome` gives each test its own `HELIOGRAPH_HOME` and library folder, and clears the process-wide view cache. Use `self.touch(rel)` and `self.index()`.
- Server tests run `server.Server` in a thread on a random port with a known token.
- Tests that need `ffmpeg` or Pillow skip when they are missing (`HAVE_FFMPEG`, `HAVE_PIL`).
- Run with `-X dev` to surface leaked file handles and unclosed databases; the suite is expected to produce no warnings.
- Browser behaviour (virtual grid, player) is verified by driving a real browser against a scratch library; see `docs/performance.md` for the scale procedure.

## Known limits

See "What was not measured" in [../performance.md](../performance.md) and the "Honest status" box in [../formats.md](../formats.md): HEIC and RAW decoding, GPU encoding on other hardware, and clean-machine million-file timings have not been verified.
