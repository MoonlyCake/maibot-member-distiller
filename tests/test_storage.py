from datetime import datetime
import os
import sqlite3
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from storage import DistillStore


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "private" / "messages.sqlite3"
        self.clock = patch("storage.time.time", return_value=1.0)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.store = DistillStore(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_global_scope_dedup_persistence_and_disabled(self):
        store = self.store
        self.assertFalse(store.record("group1", "user1", "1", "before", 1, received_at=10))
        store.set_target("user1", True)
        self.assertTrue(store.record("group1", "user1", "1", " 你好\n世界 ", 2, received_at=10))
        self.assertFalse(store.record("group1", "user1", "1", "duplicate", 2, received_at=10))
        self.assertFalse(store.record("group1", "user2", "2", "other user", 3, received_at=10))
        # Same message id in another group is distinct, and that group is covered too.
        self.assertTrue(store.record("group2", "user1", "1", "other group", 3, received_at=10))
        self.assertFalse(store.record("group1", "user1", "4", " \n ", 3, received_at=10))
        store.set_target("user1", False)
        self.assertFalse(store.record("group1", "user1", "5", "after", 4, received_at=10))
        again = DistillStore(self.db_path)
        self.assertFalse(again.is_enabled("user1"))
        self.assertEqual(again.count("user1"), 2)
        self.assertEqual(again.list_targets()[0]["count"], 2)
        self.assertEqual(again.delete_target("user1"), 2)
        self.assertEqual(again.count("user1"), 0)
        self.assertEqual(again.list_targets(), [])

    def test_export_scoped_order_and_permissions(self):
        store = self.store
        store.set_target("20", True)
        store.set_target("21", True)
        for group, mid, ts in (("10", "late", 3), ("11", "first", 1), ("10", "second", 2)):
            store.record(group, "20", mid, f"文字 {mid}", ts, received_at=10)
        store.record("10", "21", "other", "must not leak", 2, received_at=10)
        out = self.root / "exports" / "messages.txt"
        self.assertEqual(store.export_text("20", out), 3)
        text = out.read_text(encoding="utf-8")
        self.assertLess(text.index("文字 first"), text.index("文字 second"))
        self.assertLess(text.index("文字 second"), text.index("文字 late"))
        self.assertNotIn("must not leak", text)
        self.assertEqual(os.stat(out).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.db_path).st_mode & 0o777, 0o600)

    def test_cutoff_source_timestamp_and_restart(self):
        store = self.store
        with patch("storage.time.time", return_value=1000.0):
            store.set_target("u", True)
            self.assertFalse(store.record("g", "u", "pre", "backlog", 999.9, received_at=1000))
            self.assertFalse(store.record("g", "u", "missing", "unknown", None, received_at=1000))
            self.assertFalse(store.record("g", "u", "nan", "unknown", float("nan"), received_at=1000))
            self.assertFalse(store.record("g", "u", "future", "unknown", 1301, received_at=1000))
            self.assertFalse(store.record("g", "u", "delayed", "pre-received", 1000.1, received_at=999.9))
            self.assertFalse(store.record("g", "u", "bad_receive", "unknown", 1000.1, received_at=None))
            self.assertTrue(store.record("g", "u", "at", "allowed", 1000.0, received_at=1000))
            store.set_target("u", False)
        with patch("storage.time.time", return_value=2000.0):
            store.set_target("u", True)
            self.assertFalse(store.record("g", "u", "queued", "paused period", 1500.0, received_at=2000))
            self.assertTrue(store.record("g", "u", "new", "after restarting", 2000.1, received_at=2000.1))
        self.assertEqual(store.count("u"), 2)

    def test_text_export_reads_target_only_and_handles_empty(self):
        store = self.store
        store.set_target("20", True)
        store.set_target("21", True)
        store.record("10", "20", "a", "第一句\n第二行", 2, received_at=10)
        store.record("11", "20", "b", "另一群的发言", 3, received_at=10)
        store.record("10", "21", "c", "其他人的秘密", 2, received_at=10)
        out = self.root / "exports" / "corpus.txt"
        self.assertEqual(store.export_text("20", out), 2)
        text = out.read_text(encoding="utf-8")
        self.assertIn("目标 QQ：20", text)
        self.assertIn("原始文本发言", text)
        self.assertIn(f"时间：{datetime.fromtimestamp(2).astimezone().isoformat(timespec='seconds')}", text)
        self.assertIn("来源群：10", text)
        self.assertIn("来源群：11", text)
        self.assertIn("第一句\n第二行", text)
        self.assertIn("另一群的发言", text)
        self.assertNotIn("其他人的秘密", text)
        self.assertEqual(os.stat(out).st_mode & 0o777, 0o600)
        self.assertEqual(store.export_text("22", out), 0)
        self.assertIn("暂无已收集发言", out.read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            store.export_text("20", self.db_path)

    def test_symlink_database_rejected(self):
        link = self.root / "link.sqlite3"
        link.symlink_to(self.db_path)
        with self.assertRaises(ValueError):
            DistillStore(link)

    def test_context_is_per_target_group_recent_and_anonymous(self):
        store = self.store
        with patch("storage.time.time", return_value=1000.0):
            store.set_target("target", True)
            store.set_target("other_target", True)
            # This input predates the target and must never be retained.
            store.observe("g1", "sender1", "a", "before enabling", 999, received_at=999)
            store.observe("g1", "sender1", "b", "too old", 1001, received_at=1001)
            store.observe("g2", "sender2", "c", "wrong group", 1301, received_at=1301)
        with patch("storage.time.time", return_value=1305.0):
            store.observe("g1", "sender3", "d", "context one", 1302, received_at=1302)
            store.observe("g1", "target", "own", "target earlier", 1302, received_at=1302.2)
            store.observe("g1", "sender4", "e", "context two", 1303, received_at=1303)
            self.assertTrue(store.observe("g1", "target", "f", "target response", 1304, received_at=1304))
            self.assertFalse(store.observe("g1", "target", "f", "duplicate", 1304, received_at=1304))
        out = self.root / "exports" / "context.txt"
        store.export_text("target", out)
        data = out.read_text(encoding="utf-8")
        self.assertIn("context one", data)
        self.assertIn("context two", data)
        self.assertIn("[目标发言]\ntarget response", data)
        self.assertIn("上下文：其他群成员，匿名", data)
        for excluded in ("before enabling", "too old", "wrong group", "sender3", "sender4"):
            self.assertNotIn(excluded, data)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM message_context WHERE target_message_id='f'").fetchone()[0], 1)
            columns = [row[1] for row in db.execute("PRAGMA table_info(message_context)")]
            self.assertNotIn("user_id", columns)
        self.assertEqual(store.delete_target("target"), 2)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM message_context").fetchone()[0], 0)

    def test_context_backlog_out_of_order_stop_restart_and_capacity(self):
        store = self.store
        with patch("storage.time.time", return_value=1000.0):
            store.set_target("t", True)
            store.observe("g", "u", "future", "future", 1002, received_at=1002)
            store.observe("g", "u", "early", "earlier same second", 1001, received_at=1001.1)
            store.observe("g", "t", "target", "answer", 1001, received_at=1001.2)
            out = self.root / "c.txt"
            store.export_text("t", out)
            text = out.read_text(encoding="utf-8")
            self.assertIn("earlier same second", text)
            self.assertNotIn("future", text)
            store.set_target("t", False)
            self.assertEqual(len(store._context), 0)
            store.set_target("t", True)
            store.observe("g", "t", "after", "after resume", 1003, received_at=1003)
            with sqlite3.connect(self.db_path) as db:
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM message_context WHERE target_message_id='after'").fetchone()[0], 0)
            for n in range(300):
                store.observe(str(n), "u", str(n), "value", 1004, received_at=1004)
            self.assertLessEqual(len(store._context), 256)
        # Restart deliberately forgets ephemeral other-user text.
        resumed = DistillStore(self.db_path)
        self.assertEqual(len(resumed._context), 0)

    def test_context_pruning_and_unload_clear(self):
        store = self.store
        with patch("storage.time.time", return_value=1000.0):
            store.set_target("t", True)
            store.observe("g", "u", "1", "old", 1001, received_at=1001)
            store.observe("g", "u", "2", "new", 1050, received_at=1050)
        store.prune_context(now=1302)
        self.assertEqual([entry["text"] for entry in store._context[("t", "g")]], ["new"])
        store.clear_context()
        self.assertEqual(len(store._context), 0)

    def test_long_context_is_explicitly_truncated(self):
        with patch("storage.time.time", return_value=1000.0):
            self.store.set_target("t", True)
            self.store.observe("g", "u", "m", "x" * 2100, 1001, received_at=1001)
            self.store.observe("g", "t", "reply", "answer", 1002, received_at=1002)
        out = self.root / "truncated.txt"
        self.store.export_text("t", out)
        text = out.read_text(encoding="utf-8")
        self.assertIn("（上下文过长，此处截断）", text)
        self.assertNotIn("x" * 2001, text)

    def test_existing_database_is_upgraded_without_losing_messages(self):
        old = self.root / "old.sqlite3"
        with sqlite3.connect(old) as db:
            db.executescript('''
                CREATE TABLE targets (user_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE messages (group_id TEXT NOT NULL, user_id TEXT NOT NULL, message_id TEXT NOT NULL,
                    timestamp REAL NOT NULL, text TEXT NOT NULL, PRIMARY KEY (group_id, message_id));
                INSERT INTO targets VALUES ('t', 1, 1);
                INSERT INTO messages VALUES ('g', 't', 'm', 2, 'old message');
            ''')
        upgraded = DistillStore(old)
        out = self.root / "legacy.txt"
        self.assertEqual(upgraded.export_text("t", out), 1)
        self.assertIn("old message", out.read_text(encoding="utf-8"))
        self.assertTrue(upgraded.is_enabled("t"))

    def test_context_is_attached_once_during_a_burst(self):
        with patch("storage.time.time", return_value=1000.0):
            self.store.set_target("t", True)
            self.store.observe("g", "u1", "a", "路人甲", 1001, received_at=1001)
            self.store.observe("g", "u2", "b", "路人乙", 1002, received_at=1002)
            for n in range(5):
                self.assertTrue(self.store.observe("g", "t", f"t{n}", f"目标{n}", 1003 + n, received_at=1003 + n))
        with sqlite3.connect(self.db_path) as db:
            rows = db.execute("SELECT target_message_id, text FROM message_context ORDER BY position").fetchall()
        self.assertEqual(rows, [("t0", "路人甲"), ("t0", "路人乙")])

    def test_works_without_fchmod(self):
        # Windows before Python 3.13 has no os.fchmod.
        with patch("storage.os") as fake_os:
            fake_os.configure_mock(**{name: getattr(os, name) for name in (
                "open", "close", "fdopen", "fsync", "replace", "unlink",
                "O_CREAT", "O_RDWR", "O_NOFOLLOW")})
            del fake_os.fchmod
            store = DistillStore(self.root / "nofchmod.sqlite3")
            store.set_target("t", True)
            self.assertEqual(store.export_text("t", self.root / "nofchmod.txt"), 0)

    def test_adding_active_target_keeps_start_and_context(self):
        with patch("storage.time.time", return_value=1000.0):
            self.assertTrue(self.store.set_target("t", True))
            self.store.observe("g", "u", "a", "路人", 1001, received_at=1001)
        with patch("storage.time.time", return_value=1002.0):
            self.assertFalse(self.store.set_target("t", True))
            self.assertEqual(self.store._targets["t"], 1000.0)
            self.assertIn(("t", "g"), self.store._context)

    def test_target_text_is_capped(self):
        with patch("storage.time.time", return_value=1000.0):
            self.store.set_target("t", True)
            self.store.observe("g", "t", "m", "x" * 5000, 1001, received_at=1001)
        with sqlite3.connect(self.db_path) as db:
            stored = db.execute("SELECT text FROM messages").fetchone()[0]
        self.assertTrue(stored.startswith("x" * 2000))
        self.assertNotIn("x" * 2001, stored)
        self.assertIn("发言过长", stored)

    def test_forget_message_before_or_after_observe(self):
        with patch("storage.time.time", return_value=1000.0):
            self.store.set_target("t", True)
            self.store.observe("g", "t", "a", "#签到", 1001, received_at=1001)
            self.store.observe("g", "u", "b", "#打卡", 1001, received_at=1001)
            self.store.forget_message("g", "a")
            self.store.forget_message("g", "b")
            self.assertEqual(self.store.count("t"), 0)
            self.assertNotIn(("t", "g"), self.store._context)
            self.store.forget_message("g", "c")
            self.assertFalse(self.store.observe("g", "t", "c", "#迟到", 1002, received_at=1002))
            self.assertEqual(self.store.count("t"), 0)

    def test_opt_out_removes_data_and_blocks_until_opt_in(self):
        with patch("storage.time.time", return_value=1000.0):
            self.store.set_target("t", True)
            self.store.observe("g", "t", "a", "你好", 1001, received_at=1001)
            self.assertEqual(self.store.opt_out("t"), 1)
        self.assertTrue(self.store.is_opted_out("t"))
        self.assertTrue(DistillStore(self.db_path).is_opted_out("t"))
        self.assertEqual(self.store.list_targets(), [])
        self.assertFalse(self.store.has_targets)
        self.store.opt_in("t")
        self.assertFalse(self.store.is_opted_out("t"))

    def test_prune_expired(self):
        with patch("storage.time.time", return_value=1000.0):
            self.store.set_target("kept", True)
            self.store.set_target("gone", True)
            self.store.record("g", "kept", "old", "旧", 1001, received_at=1001)
            self.store.record("g", "gone", "old2", "旧", 1001, received_at=1001)
            self.store.set_target("gone", False)
        with patch("storage.time.time", return_value=1000.0 + 86400 * 2):
            self.store.record("g", "kept", "new", "新", 1000 + 86400 * 2, received_at=1000 + 86400 * 2)
            self.assertEqual(self.store.prune_expired(0), 0)
            self.assertEqual(self.store.prune_expired(1), 2)
        self.assertEqual(self.store.count("kept"), 1)
        self.assertEqual([item["user_id"] for item in self.store.list_targets()], ["kept"])

    def test_export_does_not_block_writer(self):
        with patch("storage.time.time", return_value=1000.0):
            self.store.set_target("t", True)
            self.store.record("g", "t", "a", "一", 1001, received_at=1001)
            reader = sqlite3.connect(self.db_path)
            self.addCleanup(reader.close)
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM messages").fetchone()
            self.assertTrue(self.store.record("g", "t", "b", "二", 1002, received_at=1002))
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
