#!/usr/bin/env python3
"""Time the Heliograph service on a big library: view builds, page fetches, search, folder tree, timeline.

    HELIOGRAPH_HOME=/path/to/scratch python3 tools/synth_library.py --files 1000000
    HELIOGRAPH_HOME=/path/to/scratch python3 -m heliograph serve &        # (or ./bin/heliograph --no-window)
    HELIOGRAPH_HOME=/path/to/scratch python3 tools/scale_probe.py

Prints wall time and the service's own CPU time for each call (CPU time stays meaningful when the machine
is busy with something else, wall time does not), plus the service's memory use. It only reads: it talks to the
running service over its local API with the session token from the service's state file.
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

ap = argparse.ArgumentParser(description="Time the Heliograph service on a large library.")
ap.add_argument("--root", default="/synthetic/Photos", help="library folder used for the folder-tree calls "
                "(default matches tools/synth_library.py)")
ARGS = ap.parse_args()
if "HELIOGRAPH_HOME" not in os.environ:
    sys.exit("set HELIOGRAPH_HOME to the scratch home the service was started with")
home = os.environ["HELIOGRAPH_HOME"]
ROOT = ARGS.root.rstrip("/")
try:
    info = json.load(open(home + "/state/server.json"))
except OSError:
    sys.exit(f"no running service found for HELIOGRAPH_HOME={home} (start it first, see the header of this file)")
BASE = f"http://127.0.0.1:{info['port']}"
def cpu():
    f = open(f"/proc/{info['pid']}/stat").read().rsplit(")", 1)[1].split()
    return (int(f[11]) + int(f[12])) * 10          # utime+stime in ms
LAST = [0]
def get(path, **q):
    url = BASE + path + ("?" + urllib.parse.urlencode(q) if q else "")
    c0 = cpu(); t = time.perf_counter()
    r = urllib.request.urlopen(urllib.request.Request(url, headers={"X-Heliograph-Token": info["token"]}), timeout=300)
    body = json.loads(r.read())
    LAST[0] = cpu() - c0
    return body, (time.perf_counter() - t) * 1000
def rss(): return int(open(f"/proc/{info['pid']}/status").read().split("VmRSS:")[1].split()[0]) // 1024
print(f"service RSS at start: {rss()} MB")
b, ms = get("/api/state"); print(f"state/totals            {ms:7.0f} ms  files={b['totals']['all']:,}")
for label, q in [("all, by folder, newest", dict(group="folder", sort="date", order="desc")),
                 ("all, by month, newest", dict(group="date", sort="date", order="desc")),
                 ("all, no group, by name", dict(group="none", sort="name", order="asc")),
                 ("all, no group, largest", dict(group="none", sort="size", order="desc")),
                 ("videos only, by folder", dict(group="folder", kind="video")),
                 ("search 'holiday'", dict(group="folder", q="holiday")),
                 ("search 'img_01234'", dict(group="none", q="img_01234")),
                 ("search 'holiday trip 2019'", dict(group="folder", q="holiday trip 2019")),
                 ("year 2019", dict(group="folder", year="2019")),
                 ("folder subtree /2015", dict(group="folder", dir=f"{ROOT}/2015")),
                 ("last 30 days", dict(group="folder", smart="recent"))]:
    v, ms = get("/api/view", **q)
    print(f"view {label:28s} wall {ms:7.0f} ms  CPU {LAST[0]:6d} ms  total={v['total']:>9,} groups={len(v['groups']):>6,}  RSS {rss()} MB")
    last = v
v, ms = get("/api/view", group="folder")
vid, total = v["view"], v["total"]
for off in (0, total // 2, total - 120):
    b, ms = get("/api/items", view=vid, offset=off, limit=120)
    print(f"items page @{off:>9,}      {ms:7.1f} ms  ({len(b['items'])} items)")
t = []
for i in range(20):
    b, ms = get("/api/items", view=vid, offset=(i * 49999) % total, limit=120); t.append(ms)
print(f"20 random pages: avg {sum(t)/len(t):.1f} ms, max {max(t):.1f} ms")
b, ms = get("/api/locate", view=vid, id=777777); print(f"locate id in view       {ms:7.1f} ms index={b['index']:,}")
b, ms = get("/api/ids", view=vid, **{"from": 0, "to": 20000}); print(f"ids 20,000 (select all) {ms:7.1f} ms")
b, ms = get("/api/years"); print(f"years/timeline          {ms:7.1f} ms  years={len(b['years'])}")
b, ms = get("/api/folders", dir=ROOT, covers="0", limit=400); print(f"folder tree level 1     {ms:7.1f} ms  folders={b['total']}")
b, ms = get("/api/folders", dir=f"{ROOT}/2015", limit=60); print(f"folder strip w/ covers  {ms:7.1f} ms  folders={b['total']}")
b, ms = get("/api/hb"); print(f"heartbeat               {ms:7.1f} ms")
st, ms = get("/api/status"); print(f"status                  {ms:7.1f} ms  service RSS {st['rss_kb']//1024} MB, enrich remaining {st['enrich']['remaining']:,}")
