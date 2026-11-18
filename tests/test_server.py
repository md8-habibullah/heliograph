"""The local HTTP service: routes, the token, the Host / Sec-Fetch-Site checks, range requests and thumbnails.
The server runs in a thread of the test process on a random port (no extra process, no real library)."""

import http.client
import json
import threading
import unittest

from helpers import HAVE_FFMPEG, HAVE_PIL, IsolatedHome, tiny_png

from heliograph import convert, server
from heliograph.indexer import Indexer
from heliograph.state import RUNTIME

TOKEN = "test-token"


class ServerTest(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.touch("pics/a.jpg", b"A" * 5000)
        if HAVE_PIL:
            tiny_png(str(self.lib / "pics" / "red.png"), (400, 300))
        self.touch("music/s.mp3", b"B" * 100)
        self.touch("docs/readme.txt", b"secret text")
        self.ix, self.rid = self.index()
        self.saved = dict(RUNTIME)
        self.httpd = server.Server(("127.0.0.1", 0), server.Handler)
        RUNTIME["port"] = self.httpd.server_address[1]
        RUNTIME["token"] = TOKEN
        server.CTX["indexer"], server.CTX["jobs"] = Indexer(), convert.JobManager()
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.stop)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        RUNTIME.clear()
        RUNTIME.update(self.saved)
        server.CTX["indexer"] = server.CTX["jobs"] = None

    def req(self, method, path, body=None, headers=None, token=True, host=None):
        c = http.client.HTTPConnection("127.0.0.1", RUNTIME["port"], timeout=20)
        h = {"Host": host or f"127.0.0.1:{RUNTIME['port']}"}
        if token:
            h["X-Heliograph-Token"] = TOKEN
        h.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body)
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r, raw

    def get_json(self, path, **kw):
        r, raw = self.req("GET", path, **kw)
        return r.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw)

    def first_id(self, name):
        s, view = self.get_json("/api/view?group=none&q=" + name)
        s, items = self.get_json(f"/api/items?view={view['view']}&offset=0&limit=5")
        return items["items"][0]["id"]

    # ------------------------------------------------------------------ security
    def test_api_needs_the_token(self):
        for path in ("/api/state", "/api/view", "/api/status", "/api/fs?path=/", "/api/hb", "/api/jobs"):
            self.assertEqual(self.req("GET", path, token=False)[0].status, 403, path)
        self.assertEqual(self.req("GET", "/api/state", headers={"X-Heliograph-Token": "wrong"}, token=False)[0].status, 403)
        self.assertEqual(self.req("POST", "/api/rescan", {}, token=False)[0].status, 403)
        self.assertEqual(self.req("GET", "/api/ping", token=False)[0].status, 200, "ping is how the CLI finds the service")

    def test_host_header_must_be_local(self):
        for host in ("evil.example", "evil.example:80", f"127.0.0.1.evil.example:{RUNTIME['port']}", "127.0.0.1:1"):
            self.assertEqual(self.req("GET", "/api/ping", host=host)[0].status, 403, host)
        self.assertEqual(self.req("GET", "/api/ping", host=f"localhost:{RUNTIME['port']}")[0].status, 200)

    def test_cross_site_requests_are_refused(self):
        self.assertEqual(self.req("GET", "/api/ping", headers={"Sec-Fetch-Site": "cross-site"})[0].status, 403)
        self.assertEqual(self.req("POST", "/api/rescan", {}, headers={"Sec-Fetch-Site": "same-site"})[0].status, 403)
        self.assertEqual(self.req("GET", "/api/ping", headers={"Sec-Fetch-Site": "same-origin"})[0].status, 200)

    def test_page_carries_token_and_strict_headers(self):
        r, raw = self.req("GET", "/", token=False)
        self.assertEqual(r.status, 200)
        self.assertIn(TOKEN.encode(), raw)
        self.assertNotIn(b"__TOKEN__", raw)
        csp = r.getheader("Content-Security-Policy")
        self.assertIn("default-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotIn("unsafe-eval", csp)
        self.assertIn("no-store", r.getheader("Cache-Control"))
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")

    def test_static_files(self):
        r, raw = self.req("GET", "/static/main.js", token=False)
        self.assertEqual((r.status, r.getheader("Content-Type").split(";")[0]), (200, "text/javascript"))
        for bad in ("/static/index.html", "/static/../server.py", "/static/..%2fserver.py", "/static/nope.js", "/static/%2e%2e/db.py"):
            self.assertEqual(self.req("GET", bad, token=False)[0].status, 404, bad)

    def test_files_are_served_by_id_only_and_only_if_indexed(self):
        fid = self.first_id("a.jpg")
        r, raw = self.req("GET", f"/media/{fid}")
        self.assertEqual((r.status, raw), (200, b"A" * 5000))
        self.assertEqual(self.req("GET", "/media/99999999")[0].status, 404)
        self.assertEqual(self.req("GET", "/media/abc")[0].status, 404)
        self.assertEqual(self.req("GET", "/media/../../etc/passwd")[0].status, 404)
        s, view = self.get_json("/api/view?group=none&q=readme")
        self.assertEqual(view["total"], 0, "non-media files are not in the library, so cannot be fetched")

    def test_media_is_never_executable_in_the_browser(self):
        fid = self.first_id("a.jpg")
        r, _ = self.req("GET", f"/media/{fid}")
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        self.assertIn("sandbox", (r.getheader("Content-Security-Policy") or ""), "an SVG with a script must not run")

    def test_range_requests(self):
        fid = self.first_id("a.jpg")
        r, raw = self.req("GET", f"/media/{fid}", headers={"Range": "bytes=10-19"})
        self.assertEqual((r.status, raw), (206, b"A" * 10))
        self.assertEqual(r.getheader("Content-Range"), "bytes 10-19/5000")
        r, raw = self.req("GET", f"/media/{fid}", headers={"Range": "bytes=4990-"})
        self.assertEqual((r.status, len(raw)), (206, 10))
        r, raw = self.req("GET", f"/media/{fid}", headers={"Range": "bytes=-5"})
        self.assertEqual((r.status, len(raw)), (206, 5))
        self.assertEqual(self.req("GET", f"/media/{fid}", headers={"Range": "bytes=9000-"})[0].status, 416)

    def test_bad_requests(self):
        self.assertEqual(self.req("GET", "/api/nope")[0].status, 404)
        self.assertEqual(self.req("POST", "/api/nope", {})[0].status, 404)
        c = http.client.HTTPConnection("127.0.0.1", RUNTIME["port"], timeout=10)
        c.request("POST", "/api/fav", body="{not json", headers={"Host": f"127.0.0.1:{RUNTIME['port']}", "X-Heliograph-Token": TOKEN,
                                                               "Content-Length": "9"})
        self.assertEqual(c.getresponse().status, 400)
        c.close()
        c = http.client.HTTPConnection("127.0.0.1", RUNTIME["port"], timeout=10)
        c.request("POST", "/api/fav", body="{}", headers={"Host": f"127.0.0.1:{RUNTIME['port']}", "X-Heliograph-Token": TOKEN,
                                                         "Content-Length": "99999999"})
        self.assertEqual(c.getresponse().status, 413)
        c.close()

    # ------------------------------------------------------------------ behaviour
    def test_state_view_items_item(self):
        s, st = self.get_json("/api/state")
        self.assertEqual(s, 200)
        self.assertEqual(st["totals"]["all"], 3 if HAVE_PIL else 2)
        s, view = self.get_json("/api/view?group=folder")
        s, items = self.get_json(f"/api/items?view={view['view']}&offset=0&limit=50")
        self.assertEqual(len(items["items"]), view["total"])
        s, one = self.get_json(f"/api/item/{items['items'][0]['id']}")
        self.assertEqual(s, 200)
        self.assertIn("path", one)
        self.assertEqual(self.get_json("/api/item/99999999")[0], 404)

    def test_favourite_roundtrip_over_http(self):
        fid = self.first_id("a.jpg")
        self.assertEqual(self.req("POST", "/api/fav", {"id": fid, "value": True})[0].status, 200)
        s, view = self.get_json("/api/view?smart=fav")
        self.assertEqual(view["total"], 1)
        self.assertEqual(self.req("POST", "/api/fav", {"id": fid, "value": False})[0].status, 200)

    def test_open_endpoint_refuses_system_folders(self):
        for p in ("/", "/etc", "/proc", "/sys", "/root"):
            self.assertIn(self.req("POST", "/api/open", {"path": p})[0].status, (400, 403, 404), p)

    def test_open_endpoint_with_a_real_folder(self):
        r, raw = self.req("POST", "/api/open", {"path": str(self.lib / "music"), "add": False})
        self.assertEqual(r.status, 200, raw)
        self.assertIn("root_id", json.loads(raw))

    def test_picker_lists_folders_only_in_dir_mode(self):
        s, fs = self.get_json("/api/fs?path=" + str(self.lib))
        self.assertEqual(sorted(d["name"] for d in fs["dirs"]), ["docs", "music", "pics"])

    def test_convert_validates_before_queueing(self):
        fid = self.first_id("a.jpg")
        for body in ({"ids": [fid], "preset": "evil"}, {"ids": [], "preset": "img-png"}, {"ids": [fid], "preset": "img-png", "out_dir": "/etc"},
                     {"ids": ["x"], "preset": "img-png"}, {"preset": "img-png"}):
            self.assertGreaterEqual(self.req("POST", "/api/convert", body)[0].status, 400, body)

    def test_malformed_parameters_are_400_never_500(self):
        for path, body in (("/api/fav", {"id": "x"}), ("/api/fav", {"id": [1]}), ("/api/roots/remove", {"id": "abc"}),
                           ("/api/rescan", {"id": "1; DROP"}), ("/api/open_external", {"id": 1.5}), ("/api/jobs/cancel", {"id": "z"}),
                           ("/api/convert", {"ids": ["x"], "preset": "img-png"}), ("/api/convert", {"ids": [True], "preset": "img-png"})):
            self.assertEqual(self.req("POST", path, body)[0].status, 400, (path, body))
        for path in ("/api/items?view=zzz&offset=abc", "/api/keyframe?id=x", "/api/locate?view=zzz&id=x", "/api/item/99999999999999999999"):
            self.assertIn(self.req("GET", path)[0].status, (400, 404, 410), path)

    def test_settings_are_validated(self):
        r, raw = self.req("POST", "/api/settings", {"bg_index": "paused"})
        self.assertEqual(r.status, 200, raw)
        self.assertGreaterEqual(self.req("POST", "/api/settings", {"transcode_max_height": "banana"})[0].status, 400)
        self.assertGreaterEqual(self.req("POST", "/api/settings", {"thumb_cache_mb": "-5"})[0].status, 400)
        self.assertGreaterEqual(self.req("POST", "/api/settings", {"made_up_key": "x"})[0].status, 400)
        before = self.get_json("/api/state")[1]["settings"]
        r, _ = self.req("POST", "/api/settings", {"thumb_cache_mb": "512", "transcode_gpu": "banana"})
        self.assertEqual(r.status, 400)
        self.assertEqual(self.get_json("/api/state")[1]["settings"], before, "a bad value in a batch changes nothing")
        self.assertEqual(self.req("POST", "/api/settings", {"idle_exit_min": "30"})[0].status, 200)

    def test_heartbeat_hands_over_navigation_once(self):
        self.req("POST", "/api/navigate", {"dir": str(self.lib), "file": None})
        s, hb = self.get_json("/api/hb")
        self.assertEqual(hb["nav"]["dir"], str(self.lib))
        s, hb = self.get_json("/api/hb")
        self.assertIsNone(hb["nav"])

    @unittest.skipUnless(HAVE_PIL, "needs Pillow")
    def test_thumbnail_is_a_small_jpeg_and_cached(self):
        fid = self.first_id("red.png")
        r, raw = self.req("GET", f"/thumb/{fid}")
        self.assertEqual((r.status, r.getheader("Content-Type")), (200, "image/jpeg"))
        self.assertEqual(raw[:2], b"\xff\xd8")
        self.assertLess(len(raw), 60_000)
        r2, raw2 = self.req("GET", f"/thumb/{fid}", headers={"If-None-Match": r.getheader("ETag")})
        self.assertIn(r2.status, (304, 200))
        from heliograph import thumbs
        self.assertEqual(thumbs.cache_summary()["thumbs"], 1, "the second request is answered from thumbs.db")

    def test_thumbnail_of_a_broken_picture_is_a_clean_404_and_remembered(self):
        fid = self.first_id("a.jpg")                           # 5000 'A' bytes are not a JPEG
        r, _ = self.req("GET", f"/thumb/{fid}")
        self.assertEqual(r.status, 404)
        r, _ = self.req("GET", f"/thumb/{fid}")
        self.assertEqual(r.status, 404)

    # ------------------------------------------------------------------ file ids are reused: pictures must not follow them
    def item(self, name):
        s, view = self.get_json("/api/view?group=none&q=" + name)
        s, items = self.get_json(f"/api/items?view={view['view']}&offset=0&limit=5")
        return items["items"][0]

    def test_every_item_carries_an_identity_tag_that_follows_the_file(self):
        it = self.item("a.jpg")
        self.assertRegex(it["k"], r"^[0-9a-f]{12}$")
        self.assertEqual(self.get_json(f"/api/item/{it['id']}")[1]["k"], it["k"], "the details record agrees")
        path = self.lib / "pics" / "a.jpg"
        import os
        os.utime(path, (1_500_000_000, 1_500_000_000))
        self.ix.scan(self.rid)
        self.ix.pending.clear()
        self.assertNotEqual(self.item("a.jpg")["k"], it["k"], "a changed file gets a new tag")

    @unittest.skipUnless(HAVE_PIL, "needs Pillow")
    def test_thumbnail_request_with_the_wrong_tag_is_refused_and_without_one_still_works(self):
        it = self.item("red.png")
        self.assertEqual(self.req("GET", f"/thumb/{it['id']}?k={it['k']}")[0].status, 200)
        r, _ = self.req("GET", f"/thumb/{it['id']}?k=000000000000")
        self.assertEqual(r.status, 404, "another file was remembered under this id")
        self.assertIn("no-store", r.getheader("Cache-Control") or "", "an error is never cached as if it were the picture")
        self.assertEqual(self.req("GET", f"/thumb/{it['id']}")[0].status, 200, "clients that send no tag keep working")
        self.assertEqual(self.req("GET", f"/media/{it['id']}?k=000000000000")[0].status, 404)
        self.assertEqual(self.req("GET", f"/media/{it['id']}?k={it['k']}")[0].status, 200)
        self.assertEqual(self.req("GET", f"/preview/{it['id']}?k=000000000000")[0].status, 404)

    @unittest.skipUnless(HAVE_PIL, "needs Pillow")
    def test_an_id_given_to_a_new_folder_never_shows_the_old_folders_picture(self):
        """The bug: open folder A, remove it, open folder B. B's files get A's ids, and the page (and the browser's
        one-year cache) showed A's thumbnails on B's tiles."""
        from PIL import Image
        import io
        import shutil
        from heliograph import views
        shutil.rmtree(self.lib)
        a_dir = self.lib.parent / "folder-a"
        b_dir = self.lib.parent / "folder-b"
        a_dir.mkdir()
        b_dir.mkdir()
        tiny_png(str(a_dir / "one.png"), (300, 200), color=(220, 20, 20))      # red
        tiny_png(str(b_dir / "one.png"), (300, 200), color=(20, 20, 220))      # blue
        self.ix, rid = self.index(path=a_dir)
        old = self.item("one.png")
        r, raw = self.req("GET", f"/thumb/{old['id']}?k={old['k']}")
        px = Image.open(io.BytesIO(raw)).convert("RGB").getpixel((5, 5))
        self.assertGreater(px[0], px[2] + 100, "folder A's picture is red")
        etag_a = r.getheader("ETag")
        views.remove_root(rid)
        self.ix, rid = self.index(path=b_dir)
        new = self.item("one.png")
        self.assertEqual(new["id"], old["id"], "the situation under test: the id was handed to a new file")
        self.assertNotEqual(new["k"], old["k"])
        self.assertEqual(self.req("GET", f"/thumb/{new['id']}?k={old['k']}")[0].status, 404,
                         "a page that still remembers the old file must not get the new file's picture under the old tag")
        r, raw = self.req("GET", f"/thumb/{new['id']}?k={new['k']}")
        px = Image.open(io.BytesIO(raw)).convert("RGB").getpixel((5, 5))
        self.assertGreater(px[2], px[0] + 100, "folder B's picture is blue, not A's red")
        self.assertNotEqual(r.getheader("ETag"), etag_a, "so a cached copy of A's picture can never be revalidated as B's")

    def test_state_carries_the_about_information(self):
        from heliograph import AUTHOR, AUTHOR_SITE, LICENSE_NAME, REPO_URL, formats
        s, st = self.get_json("/api/state")
        a = st["about"]
        self.assertEqual((a["author"], a["site"], a["repo"], a["license"]), (AUTHOR, AUTHOR_SITE, REPO_URL, LICENSE_NAME))
        self.assertEqual(a["formats"], {"image": len(formats.IMAGE_EXT), "video": len(formats.VIDEO_EXT), "audio": len(formats.AUDIO_EXT)})
        self.assertTrue(a["site"].startswith("https://") and a["repo"].startswith("https://github.com/"))

    def test_folder_covers_carry_the_tag_too(self):
        s, d = self.get_json("/api/folders?dir=" + str(self.lib) + "&covers=1")
        covers = [c for f in d["folders"] for c in f.get("covers", [])]
        self.assertTrue(covers)
        for c in covers:
            self.assertRegex(c["k"], r"^[0-9a-f]{12}$")
            self.assertNotIn("v", c)

    def test_stream_of_a_broken_video_fails_cleanly(self):
        if not HAVE_FFMPEG:
            self.skipTest("needs ffmpeg")
        self.touch("v/broken.mp4", b"not a video" * 100)
        self.ix.scan(self.rid)
        s, view = self.get_json("/api/view?group=none&q=broken")
        s, items = self.get_json(f"/api/items?view={view['view']}&offset=0&limit=5")
        r, raw = self.req("GET", f"/stream/{items['items'][0]['id']}?mode=transcode")
        self.assertGreaterEqual(r.status, 400)


if __name__ == "__main__":
    unittest.main()
