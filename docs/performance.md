# Performance

This page says what was measured, how, and what the numbers do **not** show. Please send your own measurements (an issue with the output of `heliograph doctor` and the numbers).

## The design targets

| Target | How it is met |
| --- | --- |
| A huge folder opens at once | The scan stores only name, size and date, and the first screen is shown before any file is opened |
| Only the surface is loaded | Virtual grid (about 35 DOM tiles however long the list is), thumbnails only for visible tiles, requests cancelled when tiles leave the screen |
| Memory stays flat | Views are arrays of 8-byte ids (1 million files = 8 MB); a page cache of 60 pages; thumbnails are blobs in a size-capped SQLite file; the browser keeps at most 700 thumbnail blob URLs |
| The PC stays usable | The governor pauses background work when CPU, disk or memory is under pressure, uses duty cycles when idle, and limits parallel thumbnail jobs to 1 to 3. The service runs at lowered CPU and I/O priority |

## Measurements

Machine: a laptop with a 13th-generation Intel Core i5, 8 GB RAM and an NVMe SSD, **while it was also running other heavy jobs** (a video-analysis script at several hundred percent CPU). Numbers are therefore conservative for wall-clock time. Where it matters, the service's own CPU time is given because it is not distorted by other load.

### A real folder: 14,016 files (13,406 pictures, 610 videos), including MOV

| Check | Result |
| --- | --- |
| DOM tiles for the whole list | 31 to 35 tiles in a 685,000 px tall scroll area |
| Thumbnail requests for the first screen | About 31 |
| A 40-jump fling across the whole list | No thumbnail requests until the scroll settles; 35 loaded within 3 s afterwards |
| Service memory | About 80 MB RSS |

### A synthetic library: 1,000,000 files

The rows were written straight into the database by [`tools/synth_library.py`](../tools/synth_library.py). The files do not exist on disk, so this measures the **index, views and API**, not thumbnail decoding or disk reads. A real million-file library will also spend time on the disk the first scan reads.

| Operation | Result |
| --- | --- |
| Database size | About 895 MB |
| Fetch a page of 120 items (any position) | 2 to 7 ms |
| Locate a file inside a view | 7 to 11 ms |
| Ids for "select all" (20,000) | 5 ms |
| Timeline (years and months) | 3 ms |
| Folder tree, one level | 32 to 60 ms |
| Heartbeat | about 1 ms |
| Search | 0.7 to 1.7 s wall under load |
| Build the default view (folder or month grouping, newest first) | 3 to 4 s of CPU under heavy competing load. It uses a covering index, so it does not sort. |
| Build a view sorted by name or size | 9 to 13 s CPU under load (about 2.5 s on a quiet machine). No index covers these; the interface shows a "Sorting N files" note while it runs |
| Service memory with six cached views | About 100 to 120 MB RSS |

Interpretation: once a view exists, moving anywhere in it costs a few milliseconds, whatever its size. Building a view over a million files is the one expensive step, it happens once per scope and library version, and it runs off the interface thread.

### What was not measured

- A clean-machine run of the million-file test (the numbers above were taken under load).
- Thumbnail throughput on a real million-picture library, and the first-scan time of a real library on a spinning disk or network share.
- Battery and power use.
- GPU use during video transcoding on other hardware.

## Reproduce

```bash
export HELIOGRAPH_HOME="$(mktemp -d)"            # never use your real library
python3 tools/synth_library.py --files 1000000   # about a minute; writes a ~900 MB database
./bin/heliograph --no-window                     # start the service on that scratch home
python3 tools/scale_probe.py                     # prints wall and CPU time for each call
./bin/heliograph monitor --once                  # service memory, governor state
./bin/heliograph stop && rm -rf "$HELIOGRAPH_HOME"
```

`tools/synth_library.py --read-fraction 1.0` (the default) marks details as already read, so the background reader does not try to open files that do not exist.

## Tuning

Run `heliograph config` to see every setting with its choices, or `heliograph config KEY VALUE` to change one. Changes apply at once; a running service reads them when it needs them.

| Setting | Default | Effect |
| --- | --- | --- |
| `bg_index` | `auto` | `paused` stops reading file details in the background (the sidebar button does the same). On-screen files are still read |
| `thumb_cache_mb` | `2048` | Size limit of the thumbnail cache; the least recently used thumbnails are removed. `heliograph cache` shows usage |
| `transcode_gpu` | `auto` | `off` never uses the GPU for live video conversion during playback |
| `transcode_max_height` | `1080` | Largest picture height for live conversion; lower it on a slow CPU |
| `idle_exit_min` | `15` | Minutes without use before the service exits (applies at its next start) |
