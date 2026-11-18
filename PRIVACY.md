# Privacy

Heliograph is a local application. **It sends nothing anywhere.** There is no account, no telemetry, no crash reporting, no update check, no remote font or script, and no network feature. The service accepts connections from `127.0.0.1` only.

## What it stores, and where

| Data | Location (default) | Notes |
| --- | --- | --- |
| Library index (file paths, names, sizes, dates, dimensions, durations, tags such as title and artist), favourites, settings, conversion history, cached stream details | `~/.local/share/heliograph/heliograph.db` | Readable only by your user (`0600`) |
| A small snapshot of library folders, favourites and settings (for recovering a damaged database) | `~/.local/share/heliograph/user-data.json` | `0600` |
| Thumbnails (400 px JPEG) and larger previews | `~/.cache/heliograph/` | A cache: safe to delete |
| Service address, process id and session token; log | `~/.local/state/heliograph/` | `0600`; the token changes at every start |

Nothing is written into your media folders except the files **you** ask for (conversion results). Heliograph never modifies, moves or deletes your original files.

Paths and tags can themselves be personal. If you share a screenshot, a bug report or the output of `heliograph search`, check it first.

## Removing it

```bash
heliograph stop
rm -rf ~/.local/share/heliograph ~/.cache/heliograph ~/.local/state/heliograph
heliograph install --uninstall      # removes the app-menu entry and icon
heliograph default --restore        # if you had made it the default application
```

## Links in the About window

The About window shows two links, the developer's website and the project's source repository. They open in your default browser only when you click them. Heliograph itself makes no request to either address.

## Other programs it starts

On your request it starts local programs: a Chromium-based browser (for the window), `ffmpeg` / `ffprobe`, and optionally ImageMagick, `heif-convert`, `dcraw_emu`, `mpv`, `xdg-open` and your file manager. Those programs have their own behaviour; in particular, a Chromium-based browser may contact its vendor's services when it starts, independently of Heliograph. If that matters to you, use a browser or profile you have configured accordingly (`HELIOGRAPH_BROWSER` selects the command).

## AI assistants

If you connect an assistant through `heliograph mcp`, the assistant can read what the tools return (file names, folders, tags, details of indexed files). What the assistant does with that is governed by the assistant and its provider, not by Heliograph. The server is read-only unless you start it with `--allow-convert`.
