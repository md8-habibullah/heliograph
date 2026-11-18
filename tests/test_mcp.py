import json
import unittest

from helpers import IsolatedHome

from heliograph import mcp


def call(tools, name, **args):
    out = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}, tools)
    r = out["result"]
    return json.loads(r["content"][0]["text"]) if not r["isError"] else r["content"][0]["text"], r["isError"]


class McpTest(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.touch("trip/holiday 1.jpg")
        self.touch("trip/holiday 2.jpg")
        self.touch("trip/clip.mp4")
        self.touch("music/song.mp3")
        self.index()
        self.tools = mcp.Tools(allow_convert=False)
        self.addCleanup(lambda: self.tools._conn and self.tools._conn.close())

    def test_handshake_and_tool_list(self):
        r = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}, self.tools)
        self.assertEqual(r["result"]["serverInfo"]["name"], "heliograph")
        self.assertIsNone(mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, self.tools))
        names = [t["name"] for t in mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, self.tools)["result"]["tools"]]
        self.assertIn("search_media", names)
        self.assertNotIn("convert_media", names, "writing tools are hidden unless the user opts in")
        on = mcp.Tools(allow_convert=True)
        names = [t["name"] for t in mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, on)["result"]["tools"]]
        self.assertIn("convert_media", names)

    def test_every_tool_has_a_valid_schema(self):
        for t in mcp.TOOLS + [mcp.CONVERT_TOOL]:
            self.assertEqual(t["inputSchema"]["type"], "object", t["name"])
            self.assertTrue(t["description"], t["name"])

    def test_status_and_search(self):
        s, err = call(self.tools, "library_status")
        self.assertFalse(err)
        self.assertEqual((s["files"], s["pictures"], s["videos"], s["audio"]), (4, 2, 1, 1))
        r, _ = call(self.tools, "search_media", query="holiday")
        self.assertEqual((r["total_matches"], r["returned"]), (2, 2))
        r, _ = call(self.tools, "search_media", query="", kind="audio")
        self.assertEqual([f["ext"] for f in r["files"]], ["mp3"])
        r, _ = call(self.tools, "search_media", limit=1, sort="name")
        self.assertEqual((r["total_matches"], r["returned"]), (4, 1))
        r, _ = call(self.tools, "search_media", folder=str(self.lib / "music"))
        self.assertEqual(r["total_matches"], 1)

    def test_folders(self):
        r, _ = call(self.tools, "list_folders")
        self.assertEqual(r["folders"][0]["files"], 4)
        r, _ = call(self.tools, "list_folders", folder=str(self.lib))
        self.assertEqual({f["path"].rsplit("/", 1)[1] for f in r["subfolders"]}, {"trip", "music"})

    def test_agents_cannot_leave_the_library(self):
        for name, args in [("search_media", {"folder": "/etc"}), ("list_folders", {"folder": "/"}),
                           ("media_info", {"path": "/etc/passwd"}), ("media_info", {"path": str(self.lib / "../../etc/passwd")})]:
            text, err = call(self.tools, name, **args)
            self.assertTrue(err, (name, args))

    def test_injection_like_input_is_just_text(self):
        r, err = call(self.tools, "search_media", query="'; DROP TABLE files; --")
        self.assertFalse(err)
        self.assertEqual(r["total_matches"], 0)
        s, _ = call(self.tools, "library_status")
        self.assertEqual(s["files"], 4)

    def test_bad_arguments_are_errors_not_crashes(self):
        self.assertTrue(call(self.tools, "search_media", limit="many")[1])
        self.assertTrue(call(self.tools, "search_media", kind="document")[1])
        out = mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "search_media", "arguments": "x"}}, self.tools)
        self.assertEqual(out["error"]["code"], -32602)
        out = mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "convert_media", "arguments": {}}}, self.tools)
        self.assertIn("error", out, "convert_media is not callable without --allow-convert")
        out = mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"}, self.tools)
        self.assertEqual(out["error"]["code"], -32601)

    def test_limit_is_capped(self):
        r, _ = call(self.tools, "search_media", limit=100000)
        self.assertLessEqual(r["returned"], mcp.MAX_LIMIT)

    def test_conversion_refused_when_disabled(self):
        text, err = mcp.Tools(allow_convert=False).convert_media.__self__, None
        with self.assertRaises(Exception):
            mcp.Tools(allow_convert=False).convert_media({"paths": [str(self.lib / "trip/clip.mp4")], "preset": "vid-mp4-h264"})


class NoLibraryTest(IsolatedHome):
    def test_helpful_error_before_anything_is_indexed(self):
        import os
        os.unlink(self.home / "data" / "heliograph.db")
        text, err = call(mcp.Tools(False), "library_status")
        self.assertTrue(err)
        self.assertIn("no library", text.lower())


if __name__ == "__main__":
    unittest.main()
