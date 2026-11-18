"""Scanning, snapshot views, paging, folders, ownership of roots - the heart of the library."""

import os
import time
import unittest

from helpers import IsolatedHome

from heliograph import db, stats, views
from heliograph.state import bump_version
from heliograph.util import ApiError


def view(**p):
    v = views.api_view(p)
    items = []
    for off in range(0, v["total"], 100):
        items += views.api_items({"view": v["view"], "offset": off, "limit": 100})["items"]
    return v, items


class LibraryTest(IsolatedHome):
    def build(self):
        old = time.mktime((2010, 6, 15, 12, 0, 0, 0, 0, -1))
        self.touch("2020/img2.jpg")
        self.touch("2020/img10.jpg")
        self.touch("2020/clip.mp4")
        self.touch("2021/Holiday Photo.png", mtime=old)
        self.touch("music/song.mp3", mtime=old)
        self.touch("music/Screenshot 2020-01-01.png")
        self.touch(".hidden/secret.jpg")
        self.touch("@eaDir/thumb.jpg")
        self.touch("notes.txt")
        self.touch("2020/.dotfile.jpg")
        return self.index()

    def test_scan_finds_only_media_and_skips_hidden_and_system_folders(self):
        self.build()
        v, items = view(group="none")
        names = sorted(i["name"] for i in items)
        self.assertEqual(names, ["Holiday Photo.png", "Screenshot 2020-01-01.png", "clip.mp4", "img10.jpg", "img2.jpg", "song.mp3"])
        self.assertEqual(stats.totals(db.conn())["all"], 6)

    def test_code_files_named_ts_are_not_videos(self):
        self.touch("app/index.ts", b"export const x: number = 1;\n" * 40)
        self.touch("app/Gtiny.ts", b"G" * 100)
        real = b"".join(b"\x47" + bytes(187) for _ in range(5))                  # five MPEG-TS packets
        self.touch("clips/real.ts", real)
        self.index()
        self.assertEqual([i["name"] for i in view(group="none")[1]], ["real.ts"])
        from heliograph.indexer import Indexer
        from heliograph import views
        with self.assertRaises(ApiError):
            views.open_file(Indexer(), str(self.lib / "app" / "index.ts"))

    def test_natural_name_sort(self):
        self.build()
        _, items = view(group="none", sort="name", order="asc", kind="image", dir=str(self.lib / "2020"))
        self.assertEqual([i["name"] for i in items], ["img2.jpg", "img10.jpg"])

    def test_grouping_by_folder_and_counts(self):
        self.build()
        v, items = view(group="folder", sort="name", order="asc")
        counts = {os.path.basename(g["key"]): g["count"] for g in v["groups"]}
        self.assertEqual(counts, {"2020": 3, "2021": 1, "music": 2})
        self.assertEqual(sum(counts.values()), v["total"])

    def test_grouping_by_type_and_month(self):
        self.build()
        v, _ = view(group="type")
        self.assertEqual([g["key"] for g in v["groups"]], ["image", "video", "audio"])
        v, _ = view(group="date")
        keys = [g["key"] for g in v["groups"]]
        self.assertIn("2010-06", keys)
        self.assertEqual(keys, sorted(keys, reverse=True), "newest month first")

    def test_filters(self):
        self.build()
        self.assertEqual(view(kind="video")[0]["total"], 1)
        self.assertEqual(view(kind="audio")[0]["total"], 1)
        self.assertEqual(view(q="holiday photo")[0]["total"], 1)
        self.assertEqual(view(q="holiday zzz")[0]["total"], 0, "every word must match")
        self.assertEqual(view(q="MUSIC")[0]["total"], 2, "search covers folders and is case-insensitive")
        self.assertEqual(view(smart="shots")[0]["total"], 1)
        self.assertEqual(view(year="2010")[0]["total"], 2)
        self.assertEqual(view(year="2010", month="6")[0]["total"], 2)
        self.assertEqual(view(year="2010", month="7")[0]["total"], 0)
        self.assertEqual(view(smart="recent")[0]["total"], 4)

    def test_subfolder_scope(self):
        self.build()
        d = str(self.lib / "2020")
        self.assertEqual(view(dir=d)[0]["total"], 3)
        self.assertEqual(view(dir=str(self.lib))[0]["total"], 6)
        self.assertEqual(view(dir=str(self.lib), sub="0")[0]["total"], 0, "only the folder itself, not its sub-folders")

    def test_search_input_is_never_sql(self):
        self.build()
        # the searched text includes the folder path (random temp name), so probe with strings that cannot be in it
        for q in ("'; DROP TABLE files; --", "%", "hel%graph", "h_liograph", '" OR 1=1 --', "x' OR '1'='1"):
            self.assertEqual(view(q=q)[0]["total"], 0, q)
        self.assertEqual(stats.totals(db.conn())["all"], 6)

    def test_paging_and_locate(self):
        for i in range(250):
            self.touch(f"bulk/p{i:03d}.jpg")
        self.index()
        v = views.api_view({"group": "none", "sort": "name", "order": "asc"})
        self.assertEqual(v["total"], 250)
        first = views.api_items({"view": v["view"], "offset": 0, "limit": 120})["items"]
        last = views.api_items({"view": v["view"], "offset": 240, "limit": 120})["items"]
        self.assertEqual((len(first), len(last)), (120, 10))
        self.assertEqual(first[0]["name"], "p000.jpg")
        self.assertEqual(views.api_locate({"view": v["view"], "id": last[3]["id"]})["index"], 243)
        self.assertEqual(views.api_locate({"view": v["view"], "id": 99999999})["index"], -1)
        self.assertEqual(len(views.api_ids({"view": v["view"], "from": 10, "to": 20})["ids"]), 10)
        with self.assertRaises(ApiError) as cm:
            views.api_items({"view": "no-such-view"})
        self.assertEqual(cm.exception.status, 410)

    def test_views_are_cached_until_the_library_changes(self):
        ix, rid = self.build()
        a = views.api_view({"group": "folder"})
        self.assertEqual(views.api_view({"group": "folder"})["view"], a["view"])
        self.touch("2020/new.jpg")
        ix.scan(rid)
        b = views.api_view({"group": "folder"})
        self.assertNotEqual(a["view"], b["view"])
        self.assertEqual(b["total"], a["total"] + 1)

    def test_rescan_removes_deleted_files(self):
        ix, rid = self.build()
        (self.lib / "2020" / "clip.mp4").unlink()
        ix.scan(rid)
        self.assertEqual(view(group="none")[0]["total"], 5)

    @unittest.skipIf(os.geteuid() == 0, "permissions are not enforced for root")
    def test_unreadable_folder_keeps_its_files(self):
        ix, rid = self.build()
        locked = self.lib / "2021"
        locked.chmod(0)
        try:
            ix.scan(rid)
            self.assertEqual(view(group="none")[0]["total"], 6, "a folder we cannot read must not be forgotten")
        finally:
            locked.chmod(0o755)

    def test_symlinks_cannot_escape_the_chosen_folder(self):
        outside = self.lib.parent / "outside"
        outside.mkdir()
        (outside / "leak.jpg").write_bytes(b"x")
        self.touch("a.jpg")
        os.symlink(outside / "leak.jpg", self.lib / "link.jpg")
        os.symlink(outside, self.lib / "dirlink")
        self.index()
        names = [i["name"] for i in view(group="none")[1]]
        self.assertEqual(names, ["a.jpg"])

    def test_session_roots_never_take_over_library_files(self):
        ix, rid = self.build()
        views.open_folder(ix, str(self.lib / "2020"), persist=False)          # opened inside the library
        ix.scan(rid)
        self.assertEqual(view(group="none")[0]["total"], 6, "still all in the library")
        c = db.conn()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM files WHERE lib=0").fetchone()[0], 0)

    def test_opened_folder_is_not_in_global_views_but_can_be_browsed(self):
        other = self.lib.parent / "other"
        other.mkdir()
        (other / "o.jpg").write_bytes(b"x")
        self.touch("a.jpg")
        ix, _ = self.index()
        ix2, _ = self.index(persist=False, path=other)
        self.assertEqual(view(group="none")[0]["total"], 1, "global views only show the library")
        self.assertEqual(view(dir=str(other))[0]["total"], 1)
        views.purge_session_roots()                                             # what the service does at start
        bump_version()
        self.assertEqual(view(dir=str(other))[0]["total"], 0)

    def test_adding_a_session_folder_to_the_library_claims_its_files(self):
        self.touch("a.jpg")
        ix, rid = self.index(persist=False)
        self.assertEqual(view(group="none")[0]["total"], 0)
        res = views.open_folder(ix, str(self.lib), persist=True)
        ix.scan(res["root_id"])
        self.assertEqual(view(group="none")[0]["total"], 1)

    def test_remove_root_forgets_files_but_not_the_disk(self):
        ix, rid = self.build()
        views.remove_root(rid)
        self.assertEqual(view(group="none")[0]["total"], 0)
        self.assertTrue((self.lib / "2020" / "img2.jpg").exists(), "files on disk are never touched")

    def test_forbidden_and_invalid_folders(self):
        for bad in ("/", "/etc", "/proc", "/usr/lib", "/does/not/exist", str(self.lib / "nope")):
            with self.assertRaises(ApiError, msg=bad):
                views.validate_dir(bad)
        views.validate_dir(str(self.lib))

    def test_open_single_file_indexes_it_at_once(self):
        p = self.touch("solo/pic.jpg")
        self.touch("solo/neighbour.jpg")
        from heliograph.indexer import Indexer
        res = views.open_file(Indexer(), str(p))
        self.assertEqual(res["dir"], str(p.parent))
        self.assertEqual(views.api_item(res["id"])["name"], "pic.jpg")
        with self.assertRaises(ApiError):
            views.open_file(Indexer(), str(self.touch("x.txt")))

    def test_favourites_follow_the_path(self):
        ix, rid = self.build()
        _, items = view(group="none", kind="video")
        fid = items[0]["id"]
        views.set_fav(fid, True)
        self.assertEqual(view(smart="fav")[0]["total"], 1)
        self.assertEqual(stats.totals(db.conn())["fav"], 1)
        ix.scan(rid)                                           # a rescan must not lose favourites
        self.assertEqual(view(smart="fav")[0]["total"], 1)
        views.set_fav(fid, False)
        self.assertEqual(view(smart="fav")[0]["total"], 0)

    def test_folder_tree_counts(self):
        self.build()
        own, subs = stats.children(db.conn(), str(self.lib))
        self.assertEqual(own, [0, 0, 0])
        by = {s["name"]: (s["i"], s["v"], s["a"]) for s in subs}
        self.assertEqual(by, {"2020": (2, 1, 0), "2021": (1, 0, 0), "music": (1, 0, 1)})
        r = views.api_folders({"dir": str(self.lib), "covers": "1"})
        self.assertTrue(all(len(f["covers"]) >= 1 for f in r["folders"]))

    def test_timeline(self):
        self.build()
        years = views.api_years()["years"]
        self.assertIn("2010", [y["year"] for y in years])

    def test_folder_picker_listing(self):
        self.touch("sub/a.jpg")
        self.touch("sub/readme.txt")
        r = views.api_fs({"path": str(self.lib)})
        self.assertEqual([d["name"] for d in r["dirs"]], ["sub"])
        self.assertEqual(r["files"], [])
        r = views.api_fs({"path": str(self.lib / "sub"), "mode": "file"})
        self.assertEqual([f["name"] for f in r["files"]], ["a.jpg"])
        with self.assertRaises(ApiError):
            views.api_fs({"path": str(self.lib / "missing")})


if __name__ == "__main__":
    unittest.main()
