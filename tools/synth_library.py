#!/usr/bin/env python3
"""Build a synthetic library of N files straight into a Heliograph database, to measure how the
app behaves with hundreds of thousands or millions of files without needing that many real files.

    HELIOGRAPH_HOME=/path/to/scratch python3 tools/synth_library.py --files 1000000
    HELIOGRAPH_HOME=/path/to/scratch heliograph serve      # then open the UI or run `heliograph monitor`

The files do not exist on disk, so thumbnails and playback will fail for them; everything else
(scrolling, grouping, search, folder tree, timeline, favourites) is real. Use a scratch HELIOGRAPH_HOME:
never point this at your real library.
"""

import argparse
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from heliograph import db, stats                      # noqa: E402
from heliograph.paths import paths                    # noqa: E402
from heliograph.util import natkey                    # noqa: E402

EXTS = [("jpg", "image", 62), ("png", "image", 10), ("heic", "image", 8), ("cr2", "image", 4),
        ("mp4", "video", 6), ("mov", "video", 4), ("mkv", "video", 1), ("mp3", "audio", 3), ("flac", "audio", 2)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--files", type=int, default=1_000_000)
    ap.add_argument("--root", default="/synthetic/Photos")
    ap.add_argument("--per-dir", type=int, default=45, help="average files per folder")
    ap.add_argument("--read-fraction", type=float, default=1.0,
                    help="share of files whose details count as already read (the rest make the background reader "
                         "try to open files that do not exist, which only adds noise to a timing run)")
    a = ap.parse_args()
    if "HELIOGRAPH_HOME" not in os.environ:
        sys.exit("refusing to run without HELIOGRAPH_HOME (this would write into your real library)")
    random.seed(7)
    paths().ensure()
    db.init()
    c = db.connect(paths().db)
    now = int(time.time())
    c.execute("BEGIN")
    rid = c.execute("INSERT OR IGNORE INTO roots(path, persist, depth, added) VALUES(?,1,-1,?)", (a.root, now)).lastrowid or \
        c.execute("SELECT id FROM roots WHERE path=?", (a.root,)).fetchone()[0]
    kinds, weights = [e[:2] for e in EXTS], [e[2] for e in EXTS]
    sql = ("INSERT OR IGNORE INTO files(root_id,lib,path,dir,name,ext,kind,size,mtime,ts,w,h,dur,name_key,dir_key,search,"
           "shot,meta,cost,seen,added) VALUES(?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,1,1,?)")
    t0 = time.time()
    made, d = 0, 0
    batch = []
    events = ["Holiday", "Birthday", "Wedding", "Trip", "Family", "Work", "Camera Roll", "Screenshots", "Friends", "Garden"]
    while made < a.files:
        year = random.randint(2004, 2026)
        month = random.randint(1, 12)
        d += 1
        folder = f"{a.root}/{year}/{year}-{month:02d}/{random.choice(events)} {d}"
        base_ts = int(time.mktime((year, month, random.randint(1, 27), 12, 0, 0, 0, 0, -1)))
        n = max(1, int(random.gauss(a.per_dir, a.per_dir / 3)))
        for i in range(n):
            ext, kind = random.choices(kinds, weights)[0]
            name = f"IMG_{random.randint(1, 99999):05d}_{d}_{i}.{ext}"
            ts = base_ts + random.randint(0, 86400 * 3)
            read = random.random() < a.read_fraction
            w, h, dur = (random.choice((4000, 3000, 1920)), random.choice((3000, 4000, 1080)), None) if read and kind == "image" \
                else (None, None, round(random.uniform(3, 600), 1)) if read and kind != "image" else (None, None, None)
            batch.append((rid, f"{folder}/{name}", folder, name, ext, kind, random.randint(10_000, 9_000_000), ts, ts, w, h, dur,
                          natkey(name), natkey(folder), f"{name} {folder}".lower(), 1 if read else 0, now))
            made += 1
            if made >= a.files:
                break
        if len(batch) >= 50_000:
            c.executemany(sql, batch)
            batch.clear()
            print(f"\r{made:>9,} files  {made / (time.time() - t0):,.0f}/s", end="", flush=True)
    if batch:
        c.executemany(sql, batch)
    c.execute("COMMIT")
    print(f"\nrows written in {time.time() - t0:.1f}s; building folder and month statistics ...")
    t1 = time.time()
    stats.refresh(c)
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    print(f"statistics in {time.time() - t1:.1f}s; database {paths().db.stat().st_size / 1e6:,.0f} MB at {paths().db}")


if __name__ == "__main__":
    main()
