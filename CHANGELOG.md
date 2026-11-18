# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - first public release

### Added
- Virtual grid that renders only what is on screen; thumbnails requested for visible tiles only, nearest the centre first, cancelled when scrolled away; a fast scroll requests nothing until it settles.
- Snapshot views (arrays of ids) so any scroll position costs a few milliseconds, even with a million files.
- SQLite library (WAL, versioned migrations, covering indexes), a separate disposable thumbnail cache, and a recovery snapshot of folders, favourites and settings.
- Resource governor (CPU, pressure-stall, memory, battery) for background work; duty-cycled reading of file details; on-screen files first.
- Open any folder or file; library folders; favourites; screenshots and "last 30 days" views; timeline; folder tree; search; grouping and sorting.
- 249 recognised extensions (121 picture, 65 video, 63 audio) including camera RAW, HEIC, JPEG XL, MOV, MKV, ProRes and many legacy formats.
- Player with direct play, remux and live conversion (GPU first, then CPU, HDR tone-mapping), seeking, audio-track and subtitle (`.srt`) support, and PCM audio inside WAV played directly.
- Conversion with FFmpeg: 22 presets, quality and size limits, GPU option, originals never touched.
- `heliograph` command: open, search, info, convert, config, cache, default, install, doctor, monitor, formats, and more.
- MCP server (`heliograph mcp`) for AI agents: read-only by default, conversion behind `--allow-convert`.
- Desktop integration: app-menu entry, "Open with", optional default-application handling that can be undone.
- Help and About window (`?`, the Help and About buttons in the sidebar, and a `?` button in the viewer): every keyboard shortcut for the library, pictures, video and music, tips, what the app handles, the developer and the source repository.
- Video keys like YouTube (`K`, `J`/`L`, `0`-`9`, `,`/`.` frame step, `Shift+,`/`Shift+.` speed, `C` subtitles, `R` repeat) and playback speed up to 3.5x. Music keys of a music player (`Shift+N`/`Shift+P`, repeat, autoplay, pitch kept at any speed) and Media Session support for media keys and desktop controls.
- Light and dark themes following the system setting.
- Test suite (standard library `unittest`), documentation, and AI-agent instructions (`AGENTS.md`, `llms.txt`).

### Fixed (found while preparing this release)
- Thumbnails of a folder that was opened earlier could show on the tiles of another folder (file ids are reused after a folder is removed, and the browser cached `/thumb/<id>` for a year). Pictures and thumbnails are now requested and cached by id plus a file tag, and the service refuses a mismatching tag.
- A large picture opened in the viewer could be shown zoomed in and cut off instead of fitted to the window.
- Opening a folder from the command line while the viewer was open left the viewer on top.

### Security
- Local-only service with `Host` and `Sec-Fetch-Site` checks, per-session token, strict Content-Security-Policy, sandboxed media responses, private file modes, and no network features.
