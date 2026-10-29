"""Private, local storage for an explicitly selected QQ user's group messages.

This module has no dependency on MaiBot and never reaches out to a network.
Timestamps are Unix seconds; time ranges are [start_time, end_time).
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from contextlib import closing
from collections import OrderedDict, deque
from datetime import datetime, timezone
from threading import RLock
from typing import Any


class DistillStore:
    def __init__(self, path: str | Path):
        self._lock = RLock()
        # Each target/group has at most two ephemeral messages; a global LRU
        # limits inactive groups, and no non-target QQ is written to disk.
        self._context: OrderedDict[tuple[str, str], deque[dict[str, Any]]] = OrderedDict()
        self._targets: dict[str, float] = {}
        self.path = Path(path).expanduser().absolute()
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("database path must not be a symlink")
        # Create with restrictive permissions before SQLite can open the file.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS targets (
                    user_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    text TEXT NOT NULL,
                    PRIMARY KEY (group_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS messages_target_time
                    ON messages (user_id, timestamp);
                CREATE TABLE IF NOT EXISTS message_context (
                    group_id TEXT NOT NULL,
                    target_message_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    timestamp REAL NOT NULL,
                    text TEXT NOT NULL,
                    PRIMARY KEY (group_id, target_message_id, position),
                    FOREIGN KEY (group_id, target_message_id)
                        REFERENCES messages(group_id, message_id) ON DELETE CASCADE
                );
            """)
            db.commit()
            self._targets = {user_id: updated_at for user_id, updated_at in db.execute(
                "SELECT user_id, updated_at FROM targets WHERE enabled=1")}

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA secure_delete = ON")
        db.execute("PRAGMA foreign_keys = ON")
        return db

    @staticmethod
    def _identifier(value: Any, field: str) -> str:
        value = str(value).strip() if value is not None else ""
        if not value or len(value) > 200 or "\x00" in value:
            raise ValueError(f"invalid {field}")
        return value

    @staticmethod
    def _timestamp(value: Any) -> float:
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("timestamp must be finite")
        return result

    def set_target(self, user_id: str, enabled: bool) -> None:
        user_id = self._identifier(user_id, "user_id")
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a boolean")
        with self._lock:
            updated_at = time.time()
            with closing(self._connect()) as db, db:
                db.execute("""INSERT INTO targets (user_id, enabled, updated_at)
                           VALUES (?, ?, ?)
                           ON CONFLICT(user_id) DO UPDATE SET
                           enabled=excluded.enabled, updated_at=excluded.updated_at""",
                           (user_id, int(enabled), updated_at))
            if enabled:
                self._targets[user_id] = updated_at
            else:
                self._targets.pop(user_id, None)
            for key in [key for key in self._context if key[0] == user_id]:
                del self._context[key]
            if not self._targets:
                self._context.clear()

    def is_enabled(self, user_id: str) -> bool:
        user_id = self._identifier(user_id, "user_id")
        with closing(self._connect()) as db:
            row = db.execute("SELECT enabled FROM targets WHERE user_id=?", (user_id,)).fetchone()
        return bool(row and row[0])

    def list_targets(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute("""SELECT t.user_id, t.enabled, t.updated_at,
                (SELECT COUNT(*) FROM messages m WHERE m.user_id=t.user_id)
                FROM targets t ORDER BY t.user_id""").fetchall()
        return [dict(user_id=u, enabled=bool(e), updated_at=updated, count=n)
                for u, e, updated, n in rows]

    def prune_context(self, now: float | None = None) -> None:
        """Forget ephemeral messages once their five-minute context window expires."""
        cutoff = self._timestamp(time.time() if now is None else now) - 300
        with self._lock:
            for key, entries in list(self._context.items()):
                retained = [entry for entry in entries if entry["received_at"] >= cutoff]
                if retained:
                    self._context[key] = deque(retained, maxlen=2)
                else:
                    del self._context[key]

    def clear_context(self) -> None:
        """Forget all transient other-user messages on unload."""
        with self._lock:
            self._context.clear()

    def record(self, group_id: str, user_id: str, message_id: str, text: str,
               timestamp: float | None, *, received_at: float,
               context: list[dict[str, Any]] | None = None) -> bool:
        """Insert a selected member's message once; return whether it was inserted."""
        group_id = self._identifier(group_id, "group_id")
        user_id = self._identifier(user_id, "user_id")
        message_id = self._identifier(message_id, "message_id")
        if not isinstance(text, str) or not text.strip():
            return False
        # Missing or implausible source timestamps cannot establish that the
        # message was sent after collection was enabled. Never replace them
        # with the receipt time. Allow a small amount of clock skew.
        if timestamp is None:
            return False
        try:
            timestamp = self._timestamp(timestamp)
            received_at = self._timestamp(received_at)
        except (TypeError, ValueError, OverflowError):
            return False
        now = time.time()
        if (timestamp <= 0 or timestamp > now + 300 or received_at <= 0
                or received_at > now + 300):
            return False
        with self._lock, closing(self._connect()) as db, db:
            cursor = db.execute("""INSERT OR IGNORE INTO messages
                (group_id, user_id, message_id, timestamp, text)
                SELECT ?, ?, ?, ?, ? WHERE EXISTS
                (SELECT 1 FROM targets WHERE user_id=? AND enabled=1 AND ? >= updated_at AND ? >= updated_at)""",
                (group_id, user_id, message_id, timestamp, text, user_id, timestamp, received_at))
            if cursor.rowcount != 1:
                return False
            for position, item in enumerate(context or []):
                db.execute("INSERT INTO message_context VALUES (?, ?, ?, ?, ?)",
                           (group_id, message_id, position, item["timestamp"], item["text"]))
            return True

    def observe(self, group_id: str, user_id: str, message_id: str, text: str,
                timestamp: float | None, *, received_at: float) -> bool:
        """Observe one valid group text. Only targets and attached context reach disk."""
        group_id = self._identifier(group_id, "group_id")
        user_id = self._identifier(user_id, "user_id")
        message_id = self._identifier(message_id, "message_id")
        if not isinstance(text, str) or not text.strip() or timestamp is None:
            return False
        try:
            timestamp = self._timestamp(timestamp)
            received_at = self._timestamp(received_at)
        except (TypeError, ValueError, OverflowError):
            return False
        now = time.time()
        if timestamp <= 0 or received_at <= 0 or timestamp > now + 300 or received_at > now + 300:
            return False
        with self._lock:
            start = self._targets.get(user_id)
            recorded = False
            if start is not None:
                # Both source and receipt times ensure that backlog is not collected.
                candidates = self._context.get((user_id, group_id), ())
                context = [entry for entry in candidates
                           if start <= entry["timestamp"] <= timestamp
                           and start <= entry["received_at"] < received_at
                           and received_at - entry["received_at"] <= 300
                           and timestamp - entry["timestamp"] <= 300]
                context.sort(key=lambda entry: (entry["received_at"], entry["timestamp"]))
                recorded = self.record(group_id, user_id, message_id, text, timestamp,
                                       received_at=received_at, context=context[-2:])
            # Keep a separate bounded buffer for each target, so its own earlier
            # messages never displace the surrounding messages of other users.
            for target, since in self._targets.items():
                if target == user_id or timestamp < since or received_at < since:
                    continue
                key = (target, group_id)
                entries = self._context.setdefault(key, deque(maxlen=2))
                self._context.move_to_end(key)
                # Ignore a duplicate delivery; IDs are kept only in memory.
                if not any(item["message_id"] == message_id for item in entries):
                    context_text = text if len(text) <= 2000 else text[:2000] + "\n（上下文过长，此处截断）"
                    candidates = list(entries)
                    candidates.append({"message_id": message_id, "timestamp": timestamp,
                                       "received_at": received_at, "text": context_text})
                    candidates.sort(key=lambda item: (item["received_at"], item["timestamp"]))
                    self._context[key] = deque(candidates[-2:], maxlen=2)
                while len(self._context) > 256:
                    self._context.popitem(last=False)
            return recorded

    def count(self, user_id: str) -> int:
        user_id = self._identifier(user_id, "user_id")
        with closing(self._connect()) as db:
            return db.execute("SELECT COUNT(*) FROM messages WHERE user_id=?", (user_id,)).fetchone()[0]

    def delete_messages(self, user_id: str) -> int:
        user_id = self._identifier(user_id, "user_id")
        with self._lock, closing(self._connect()) as db, db:
            return db.execute("DELETE FROM messages WHERE user_id=?", (user_id,)).rowcount

    def export_jsonl(self, user_id: str, path: str | Path,
                     start_time: float | None = None, end_time: float | None = None) -> int:
        """Export real target messages in time order, with source metadata, atomically."""
        user_id = self._identifier(user_id, "user_id")
        start = self._timestamp(start_time) if start_time is not None else None
        end = self._timestamp(end_time) if end_time is not None else None
        if start is not None and end is not None and start >= end:
            raise ValueError("start_time must precede end_time")
        path = Path(path).expanduser().absolute()
        if path == self.path or path.resolve() == self.path.resolve():
            raise ValueError("export path must differ from database path")
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        sql = "SELECT group_id, user_id, message_id, timestamp, text FROM messages WHERE user_id=?"
        values: list[Any] = [user_id]
        if start is not None:
            sql += " AND timestamp >= ?"
            values.append(start)
        if end is not None:
            sql += " AND timestamp < ?"
            values.append(end)
        sql += " ORDER BY timestamp, rowid"
        fd, temp_name = tempfile.mkstemp(prefix=".distill-", suffix=".tmp", dir=path.parent)
        count = 0
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output, closing(self._connect()) as db:
                for row in db.execute(sql, values):
                    item = dict(zip(("group_id", "user_id", "message_id", "timestamp", "text"), row))
                    item["context"] = [dict(timestamp=stamp, text=text) for stamp, text in db.execute(
                        "SELECT timestamp, text FROM message_context WHERE group_id=? AND target_message_id=? ORDER BY position",
                        (item["group_id"], item["message_id"]))]
                    output.write(json.dumps(item, ensure_ascii=False) + "\n")
                    count += 1
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_name, path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise
        return count

    def export_text(self, user_id: str, path: str | Path) -> int:
        """Export the selected user's original group messages as readable UTF-8."""
        user_id = self._identifier(user_id, "user_id")
        path = Path(path).expanduser().absolute()
        if path == self.path or path.resolve() == self.path.resolve():
            raise ValueError("export path must differ from database path")
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".distill-", suffix=".tmp", dir=path.parent)
        count = 0
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output, closing(self._connect()) as db:
                output.write(f"目标 QQ：{user_id}\n")
                output.write("以下是已收集的该目标在群聊中的原始文本发言，按时间排列。时间为 UTC。\n")
                output.write("每条目标发言前可能包含同群最近两条其他成员的匿名上下文；请勿把上下文当作目标发言。\n")
                for group_id, message_id, timestamp, text in db.execute(
                    "SELECT group_id, message_id, timestamp, text FROM messages WHERE user_id=? ORDER BY timestamp, rowid",
                    (user_id,),
                ):
                    count += 1
                    stamp = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")
                    output.write(f"\n--- 发言 {count} | 时间：{stamp} | 来源群：{group_id} | 消息 ID：{message_id} ---\n")
                    for context_time, context_text in db.execute(
                        "SELECT timestamp, text FROM message_context WHERE group_id=? AND target_message_id=? ORDER BY position",
                        (group_id, message_id),
                    ):
                        context_stamp = datetime.fromtimestamp(context_time, tz=timezone.utc).isoformat(timespec="seconds")
                        output.write(f"[上下文：其他群成员，匿名 | 时间：{context_stamp}]\n{context_text}\n")
                    output.write("[目标发言]\n")
                    output.write(text)
                    output.write("\n")
                if not count:
                    output.write("\n（暂无已收集发言。）\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_name, path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise
        return count
