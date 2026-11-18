# Frequently asked questions

**Does it upload anything?** No. Heliograph has no network features and no telemetry. It listens on `127.0.0.1` for its own window. See [PRIVACY.md](../PRIVACY.md).

**Why a browser window instead of a native toolkit?** A Chromium-based browser already decodes H.264, VP9, AV1, AAC, Opus and FLAC with hardware help, scales and caches images well, and gives a fast virtual list. Heliograph runs it in app mode (no tabs, no address bar) and adds a small Python service for everything a browser cannot do (indexing, FFmpeg, local files). The trade-off is that a Chromium-based browser must be installed.

**It says "Details on hold". Is something wrong?** No. Reading the details of every file is background work, and the resource governor pauses it while your PC is busy (high CPU, busy disk, memory pressure, or on battery it is gentler). Files on screen are always read at once. Use "Pause background work" to stop it entirely, or `heliograph monitor` to watch it.

**My HEIC or RAW pictures show a placeholder.** They need a decoder on your machine: FFmpeg built with HEIF support, `heif-convert` (from libheif), ImageMagick, or `dcraw_emu` (LibRaw). Run `heliograph formats` to see what was found. These paths have not been tested on real camera files yet, so please open an issue with the file type and `heliograph info FILE` output.

**A video will not play.** Press `I` in the viewer. The Playback section says how the file is played and why. If it says "Converted live" and it stutters, lower the live-conversion height: `heliograph config transcode_max_height 720`. If it says it could not be read, the file may be damaged; the viewer offers to open it with another app.

**Where are the keyboard shortcuts?** Press `?` anywhere in the window, or click **Help** at the bottom of the sidebar or the `?` button in the viewer. Video uses the same keys as YouTube (`K`, `J`/`L`, `0`-`9`, `,`/`.`, `Shift+,`/`Shift+.` for speed up to 3.5x). The same window has an **About** tab.

**A thumbnail shows the wrong picture.** Thumbnails are cached by file identity, so this should not happen: if it does, please open an issue. `heliograph cache clear` throws every thumbnail away and they are rebuilt as you scroll.

**Can it be my default image viewer and video player?** Yes: `heliograph default --images --videos --audio`. It remembers your previous choices; `heliograph default --restore` undoes it. `heliograph install` alone only adds Heliograph to the app menu and "Open with".

**Does converting change my originals?** Never. Results are new files, written to a `.part` file first and renamed when complete. A name clash gets a ` (1)` suffix.

**How do I delete everything it stored?** `heliograph stop`, then delete `~/.local/share/heliograph`, `~/.cache/heliograph` and `~/.local/state/heliograph`. The thumbnail cache alone can be cleared with `heliograph cache clear`.

**Where is my data?** The index, favourites and settings are in `~/.local/share/heliograph/heliograph.db` (plus a small `user-data.json` snapshot). Thumbnails are in `~/.cache/heliograph/thumbs.db`. Nothing is stored inside your media folders.

**What happens if the database is damaged?** On the next start Heliograph checks it, moves the damaged file aside (`heliograph.db.corrupt-<time>`), creates a new one, restores your folders, favourites and settings from the snapshot, and rescans.

**Does it work on Wayland?** Yes: it was run on Hyprland (Wayland) with Brave and with Chromium, and opening a folder or a file from the command line reuses the open window. One caveat: on Wayland a Chromium-based browser ignores the `--class` option and names the window itself (for example `brave-127.0.0.1__-Default`), so the window class is *not* `dev.habibullah.Heliograph` there. `heliograph install` works out the name for your browser and puts it in the launcher (`StartupWMClass`), so the app menu and taskbar match the window. If your shell or taskbar shows a stray icon, open the window, find its class (`hyprctl clients`, or `xprop WM_CLASS` on X11) and run `heliograph install --wm-class THE-CLASS`. Other compositors have not been tried.

**Which Linux distributions does it run on?** Any with Python 3.10 or newer, FFmpeg and a Chromium-based browser. Each release has an install file for Debian-family systems (`.deb`: Ubuntu, Debian, Linux Mint, Pop!_OS), RPM-based systems (`.rpm`: Fedora, openSUSE) and Arch-family systems (`.pkg.tar.zst`: Arch, CachyOS, Manjaro), and `pipx` covers the rest. One command per family is in [install.md](install.md), together with what was tested on which distribution. The desktop window itself has only been tried on Hyprland.

**Can I install it from apt, dnf or pacman instead of downloading a file?** Not yet. Heliograph is not in any distribution's repositories, so you install the downloaded file (updates mean downloading the newer file). Getting into those repositories is possible and [packaging.md](packaging.md) lists each route with what it takes: the AUR and Fedora COPR in an afternoon, a self-hosted apt/dnf repository in a few days, the official Debian, Fedora and Arch archives in weeks to months.

**Does it support Windows or macOS?** Not at the moment. It is built for Linux (it uses `/proc`, XDG folders and `xdg-mime`). Contributions are welcome; see [CONTRIBUTING.md](../CONTRIBUTING.md).

**Why 249 formats and not "everything"?** The list covers the common and the long-tail picture, video and audio extensions. Whether a given file works depends on its codec and your decoders, which Heliograph checks per file. If something should be recognised and is not, add its extension in `src/heliograph/formats.py` (a one-line change) or open an issue.
