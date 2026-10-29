import json
import os
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
        self.assertEqual(again.delete_messages("user1"), 2)
        self.assertEqual(again.count("user1"), 0)

    def test_export_scoped_order_range_and_permissions(self):
        store = self.store
        store.set_target("20", True)
        store.set_target("21", True)
        for group, mid, ts in (("10", "late", 3), ("11", "first", 1), ("10", "second", 2)):
            store.record(group, "20", mid, f"文字 {mid}\n第二行", ts, received_at=10)
        store.record("10", "21", "other", "must not leak", 2, received_at=10)
        out = self.root / "exports" / "messages.jsonl"
        self.assertEqual(store.export_jsonl("20", out, 1, 3), 2)
        rows = [json.loads(line) for line in out.read_text().splitlines()]
        self.assertEqual([r["message_id"] for r in rows], ["first", "second"])
        self.assertEqual(rows[0]["text"], "文字 first\n第二行")
        self.assertEqual(rows[0]["group_id"], "11")
        self.assertEqual(rows[0]["user_id"], "20")
        self.assertEqual(os.stat(out).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.db_path).st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            store.export_jsonl("20", out, 3, 3)
        with self.assertRaises(ValueError):
            store.export_jsonl("20", self.db_path)

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
        self.assertIn("时间：1970-01-01T00:00:02+00:00", text)
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


if __name__ == "__main__":
    unittest.main()
