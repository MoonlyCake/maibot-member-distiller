"""Private, local storage for an explicitly selected QQ user's group messages.

This module has no dependency on MaiBot and never reaches out to a network.
Timestamps are Unix seconds.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from contextlib import closing
from collections import OrderedDict, deque
from datetime import datetime
from threading import RLock
from typing import Any

_MAX_TEXT = 2000
_MAX_FORGOTTEN = 512


def _restrict(fd: int) -> None:
    # Windows has no os.fchmod before Python 3.13; access there follows the directory's ACL.
    if hasattr(os, "fchmod"):
        os.fchmod(fd, 0o600)


def _clip(text: str, note: str) -> str:
    return text if len(text) <= _MAX_TEXT else f"{text[:_MAX_TEXT]}\n（{note}）"


def _local_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


class DistillStore:
    def __init__(self, path: str | Path):
        self._lock = RLock()
        # Each target/group has at most two ephemeral messages; a global LRU
        # limits inactive groups, and no non-target QQ is written to disk.
        self._context: OrderedDict[tuple[str, str], deque[dict[str, Any]]] = OrderedDict()
        self._targets: dict[str, float] = {}
        # Commands can be recognised after the message was observed, or before.
        self._forgotten: OrderedDict[tuple[str, str], None] = OrderedDict()
        self.path = Path(path).expanduser().absolute()
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("database path must not be a symlink")
        # Create with restrictive permissions before SQLite can open the file.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            _restrict(fd)
        finally:
            os.close(fd)
        with closing(self._connect()) as db:
            # WAL lets a long export read without blocking collection writes.
            db.execute("PRAGMA journal_mode = WAL")
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
                CREATE TABLE IF NOT EXISTS opted_out (
                    user_id TEXT PRIMARY KEY,
                    opted_out_at REAL NOT NULL
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

    def _checkpoint(self) -> None:
        """Move deletions out of the WAL so removed text does not linger in it."""
        with closing(self._connect()) as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()

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

    @classmethod
    def _message_times(cls, timestamp: Any, received_at: Any) -> tuple[float, float] | None:
        # Missing or implausible source timestamps cannot establish that the
        # message was sent after collection was enabled. Never replace them
        # with the receipt time. Allow a small amount of clock skew.
        try:
            timestamp, received_at = cls._timestamp(timestamp), cls._timestamp(received_at)
        except (TypeError, ValueError, OverflowError):
            return None
        now = time.time()
        if timestamp <= 0 or received_at <= 0 or timestamp > now + 300 or received_at > now + 300:
            return None
        return timestamp, received_at

    @property
    def has_targets(self) -> bool:
        return bool(self._targets)

    def _drop_target_state(self, user_id: str) -> None:
        self._targets.pop(user_id, None)
        for key in [key for key in self._context if key[0] == user_id]:
            del self._context[key]
        if not self._targets:
            self._context.clear()

    def set_target(self, user_id: str, enabled: bool) -> bool:
        """Start or stop collection; return False when an active target is added again."""
        user_id = self._identifier(user_id, "user_id")
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a boolean")
        with self._lock:
            # Adding again keeps the original start time and buffered context.
            if enabled and user_id in self._targets:
                return False
            updated_at = time.time()
            with closing(self._connect()) as db, db:
                db.execute("""INSERT INTO targets (user_id, enabled, updated_at)
                           VALUES (?, ?, ?)
                           ON CONFLICT(user_id) DO UPDATE SET
                           enabled=excluded.enabled, updated_at=excluded.updated_at""",
                           (user_id, int(enabled), updated_at))
            self._drop_target_state(user_id)
            if enabled:
                self._targets[user_id] = updated_at
            return True

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

    def forget_message(self, group_id: str, message_id: str) -> None:
        """Remove a message later recognised as a command, and refuse it if it arrives after this."""
        group_id = self._identifier(group_id, "group_id")
        message_id = self._identifier(message_id, "message_id")
        with self._lock:
            self._forgotten[(group_id, message_id)] = None
            while len(self._forgotten) > _MAX_FORGOTTEN:
                self._forgotten.popitem(last=False)
            for key in [key for key in self._context if key[1] == group_id]:
                self._discard_context(key, {message_id})
            with closing(self._connect()) as db, db:
                db.execute("DELETE FROM messages WHERE group_id=? AND message_id=?", (group_id, message_id))

    def _discard_context(self, key: tuple[str, str], message_ids: set[str]) -> None:
        retained = [entry for entry in self._context.get(key, ()) if entry["message_id"] not in message_ids]
        if retained:
            self._context[key] = deque(retained, maxlen=2)
        else:
            self._context.pop(key, None)

    def _insert(self, group_id: str, user_id: str, message_id: str, text: str, timestamp: float,
                received_at: float, context: list[dict[str, Any]] | None) -> bool:
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

    def record(self, group_id: str, user_id: str, message_id: str, text: str,
               timestamp: float | None, *, received_at: float,
               context: list[dict[str, Any]] | None = None) -> bool:
        """Insert a selected member's message once; return whether it was inserted."""
        group_id = self._identifier(group_id, "group_id")
        user_id = self._identifier(user_id, "user_id")
        message_id = self._identifier(message_id, "message_id")
        times = self._message_times(timestamp, received_at)
        if not isinstance(text, str) or not text.strip() or times is None:
            return False
        return self._insert(group_id, user_id, message_id, text, *times, context)

    def observe(self, group_id: str, user_id: str, message_id: str, text: str,
                timestamp: float | None, *, received_at: float) -> bool:
        """Observe one valid group text. Only targets and attached context reach disk."""
        group_id = self._identifier(group_id, "group_id")
        user_id = self._identifier(user_id, "user_id")
        message_id = self._identifier(message_id, "message_id")
        times = self._message_times(timestamp, received_at)
        if not isinstance(text, str) or not text.strip() or times is None:
            return False
        timestamp, received_at = times
        with self._lock:
            if (group_id, message_id) in self._forgotten:
                return False
            start = self._targets.get(user_id)
            recorded = False
            if start is not None:
                # Both source and receipt times ensure that backlog is not collected.
                key = (user_id, group_id)
                candidates = self._context.get(key, ())
                context = [entry for entry in candidates
                           if start <= entry["timestamp"] <= timestamp
                           and start <= entry["received_at"] < received_at
                           and received_at - entry["received_at"] <= 300
                           and timestamp - entry["timestamp"] <= 300]
                context.sort(key=lambda entry: (entry["received_at"], entry["timestamp"]))
                context = context[-2:]
                recorded = self._insert(group_id, user_id, message_id, _clip(text, "发言过长，此处截断"),
                                        timestamp, received_at, context)
                # Attach each message once, so a burst from the target does not repeat it.
                if recorded:
                    self._discard_context(key, {entry["message_id"] for entry in context})
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
                    candidates = list(entries)
                    candidates.append({"message_id": message_id, "timestamp": timestamp,
                                       "received_at": received_at,
                                       "text": _clip(text, "上下文过长，此处截断")})
                    candidates.sort(key=lambda item: (item["received_at"], item["timestamp"]))
                    self._context[key] = deque(candidates[-2:], maxlen=2)
                while len(self._context) > 256:
                    self._context.popitem(last=False)
            return recorded

    def count(self, user_id: str) -> int:
        user_id = self._identifier(user_id, "user_id")
        with closing(self._connect()) as db:
            return db.execute("SELECT COUNT(*) FROM messages WHERE user_id=?", (user_id,)).fetchone()[0]

    def delete_target(self, user_id: str) -> int:
        """Stop collecting and remove the target with all of its messages."""
        user_id = self._identifier(user_id, "user_id")
        with self._lock:
            with closing(self._connect()) as db, db:
                removed = db.execute("DELETE FROM messages WHERE user_id=?", (user_id,)).rowcount
                db.execute("DELETE FROM targets WHERE user_id=?", (user_id,))
            self._drop_target_state(user_id)
            self._checkpoint()
            return removed

    def is_opted_out(self, user_id: str) -> bool:
        user_id = self._identifier(user_id, "user_id")
        with closing(self._connect()) as db:
            return db.execute("SELECT 1 FROM opted_out WHERE user_id=?", (user_id,)).fetchone() is not None

    def opt_out(self, user_id: str) -> int:
        """Delete a user's data at their own request and refuse later collection."""
        user_id = self._identifier(user_id, "user_id")
        with self._lock:
            with closing(self._connect()) as db, db:
                db.execute("INSERT OR REPLACE INTO opted_out VALUES (?, ?)", (user_id, time.time()))
            return self.delete_target(user_id)

    def opt_in(self, user_id: str) -> None:
        """Lift the user's own refusal; collection needs a new explicit add."""
        user_id = self._identifier(user_id, "user_id")
        with self._lock, closing(self._connect()) as db, db:
            db.execute("DELETE FROM opted_out WHERE user_id=?", (user_id,))

    def prune_expired(self, retention_days: float, now: float | None = None) -> int:
        """Remove messages past retention and stopped targets that have none left."""
        if retention_days <= 0:
            return 0
        cutoff = self._timestamp(time.time() if now is None else now) - retention_days * 86400
        with self._lock:
            with closing(self._connect()) as db, db:
                removed = db.execute("DELETE FROM messages WHERE timestamp < ?", (cutoff,)).rowcount
                db.execute("""DELETE FROM targets WHERE enabled=0 AND user_id NOT IN
                           (SELECT user_id FROM messages)""")
            if removed:
                self._checkpoint()
            return removed

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
            _restrict(fd)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output, closing(self._connect()) as db:
                # One read snapshot, without blocking writers under WAL.
                db.execute("BEGIN")
                output.write(f"目标 QQ：{user_id}\n")
                output.write("以下是已收集的该目标在群聊中的原始文本发言，按时间排列。时间为机器人本机时区。\n")
                output.write("每条目标发言前可能包含同群最近两条其他成员的匿名上下文；请勿把上下文当作目标发言。\n")
                for group_id, message_id, timestamp, text in db.execute(
                    "SELECT group_id, message_id, timestamp, text FROM messages WHERE user_id=? ORDER BY timestamp, rowid",
                    (user_id,),
                ):
                    count += 1
                    output.write(f"\n--- 发言 {count} | 时间：{_local_time(timestamp)} | 来源群：{group_id} | 消息 ID：{message_id} ---\n")
                    for context_time, context_text in db.execute(
                        "SELECT timestamp, text FROM message_context WHERE group_id=? AND target_message_id=? ORDER BY position",
                        (group_id, message_id),
                    ):
                        output.write(f"[上下文：其他群成员，匿名 | 时间：{_local_time(context_time)}]\n{context_text}\n")
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
