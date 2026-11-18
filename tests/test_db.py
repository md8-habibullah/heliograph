import os
import unittest

from helpers import IsolatedHome

from heliograph import db
from heliograph.paths import paths


class DbTest(IsolatedHome):
    def test_schema_and_idempotent_init(self):
        c = db.connect()
        self.addCleanup(c.close)
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"roots", "files", "dirstats", "monthstats", "favourites", "settings", "probes", "jobs"} <= names)
        db.init()
        db.init(check=True)
        self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(c.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_files_are_private(self):
        mode = os.stat(paths().db).st_mode & 0o777
        self.assertEqual(mode & 0o077, 0, "the database must not be readable by other users")
        self.assertEqual(os.stat(paths().data).st_mode & 0o077, 0)

    def test_settings(self):
        self.assertEqual(db.get_setting("bg_index"), "auto")
        db.set_setting("bg_index", "paused")
        self.assertEqual(db.get_setting("bg_index"), "paused")
        self.assertEqual(db.all_settings()["bg_index"], "paused")
        self.assertEqual(db.get_setting("nope", "dflt"), "dflt")

    def test_snapshot_restore_roundtrip(self):
        c = db.conn()
        c.execute("INSERT INTO roots(path, persist, depth, added) VALUES('/a', 1, -1, 1)")
        c.execute("INSERT INTO roots(path, persist, depth, added) VALUES('/session', 0, -1, 1)")
        c.execute("INSERT INTO favourites(path, added) VALUES('/a/x.jpg', 5)")
        db.set_setting("transcode_max_height", "720")
        db.write_user_data()
        snap = paths().user_json.read_text()
        self.assertIn("/a/x.jpg", snap)
        self.assertNotIn("/session", snap, "folders opened for one session are not part of the saved data")
        self.assertEqual(os.stat(paths().user_json).st_mode & 0o077, 0)
        db.close_thread_connection()
        for suffix in ("", "-wal", "-shm"):
            p = paths().db.with_name(paths().db.name + suffix)
            if p.exists():
                p.unlink()
        db.init()
        self.assertTrue(db.restore_user_data())
        c = db.conn()
        self.assertEqual([r[0] for r in c.execute("SELECT path FROM roots")], ["/a"])
        self.assertEqual([r[0] for r in c.execute("SELECT path FROM favourites")], ["/a/x.jpg"])
        self.assertEqual(db.get_setting("transcode_max_height"), "720")

    def test_corrupt_database_is_set_aside_and_rebuilt(self):
        c = db.conn()
        c.execute("INSERT INTO roots(path, persist, depth, added) VALUES('/keep', 1, -1, 1)")
        db.write_user_data()
        db.close_thread_connection()
        for suffix in ("-wal", "-shm"):
            p = paths().db.with_name(paths().db.name + suffix)
            if p.exists():
                p.unlink()
        with open(paths().db, "r+b") as f:                     # damage the file
            f.seek(0)
            f.write(b"this is not a database" * 50)
        status = db.init(check=True)
        self.assertTrue(status.startswith("recovered"), status)
        self.assertTrue(list(paths().data.glob("heliograph.db.corrupt-*")), "the damaged file is kept for inspection")
        c = db.connect()
        self.addCleanup(c.close)
        self.assertEqual(c.execute("PRAGMA quick_check").fetchone()[0], "ok")
        self.assertEqual([r[0] for r in c.execute("SELECT path FROM roots")], ["/keep"], "user data restored from the snapshot")

    def test_integrity_report(self):
        ok, msg = db.integrity_report()
        self.assertTrue(ok, msg)


if __name__ == "__main__":
    unittest.main()
