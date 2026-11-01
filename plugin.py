"""在所有 QQ 群中收集指定用户今后的文字，供本地整理。"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
import re
import time
from typing import Any
from uuid import uuid4

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode

from .storage import DistillStore

_COMMAND_PATTERN = (r"(?i)^/distill(?:\s+(?P<action>add|stop|status|export|delete|list|optout|optin)"
                    r"(?:\s+(?P<qq>[0-9]+))?)?\s*$")
_COMMAND = re.compile(_COMMAND_PATTERN)
_ID = re.compile(r"^[0-9]+$")
_SELF_ACTIONS = {"optout", "optin"}
_MAINTENANCE_TICKS = 60


def _message_parts(message: Any) -> tuple[str, str, str] | None:
    """从同一条 QQ 群消息核实来源；私聊和不完整的来源直接忽略。"""
    if not isinstance(message, dict) or message.get("platform") != "qq":
        return None
    info = message.get("message_info")
    if not isinstance(info, dict) or (info.get("platform") and info["platform"] != "qq"):
        return None
    user, group = info.get("user_info"), info.get("group_info")
    if not isinstance(user, dict) or not isinstance(group, dict):
        return None
    user_id = str(user.get("user_id") or "")
    group_id = str(group.get("group_id") or "")
    message_id = str(message.get("message_id") or "")
    if not _ID.fullmatch(user_id) or not _ID.fullmatch(group_id) or not message_id:
        return None
    return group_id, user_id, message_id


def _command_parts(message: Any) -> tuple[str, str, str] | None:
    """管理命令可在 QQ 私聊或群聊发出，收集范围始终只包括群聊。"""
    group_parts = _message_parts(message)
    if group_parts is not None:
        return group_parts
    if not isinstance(message, dict) or message.get("platform") != "qq":
        return None
    info = message.get("message_info")
    if (not isinstance(info, dict) or "group_info" not in info or info["group_info"] is not None
            or (info.get("platform") and info["platform"] != "qq")):
        return None
    user = info.get("user_info")
    if not isinstance(user, dict):
        return None
    user_id = str(user.get("user_id") or "")
    message_id = str(message.get("message_id") or "")
    if not _ID.fullmatch(user_id) or not message_id:
        return None
    return "", user_id, message_id


def _actual_text(message: Any) -> str:
    """只取本条消息的文字段，排除引用、转发和媒体派生内容。"""
    if not isinstance(message, dict) or not isinstance(message.get("raw_message"), list):
        return ""
    pieces = [segment["data"] for segment in message["raw_message"]
              if isinstance(segment, dict) and segment.get("type") == "text"
              and isinstance(segment.get("data"), str)]
    return "".join(pieces).strip()


def _remove_exports(exports: Path, *, qq: str = "", older_than: float | None = None) -> int:
    """删除指定 QQ 的导出文件，或超过保留期的全部导出文件。"""
    removed = 0
    for path in exports.glob(f"qq_{qq}_*.txt" if qq else "qq_*.txt"):
        if older_than is None or path.stat().st_mtime < older_than:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


class PluginSettings(PluginConfigBase):
    enabled: bool = Field(default=False, description="启用群友发言收集插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class CollectorSettings(PluginConfigBase):
    allowed_qq: list[str] = Field(default_factory=list, description="允许管理的 QQ；留空时拒绝所有人")
    retention_days: int = Field(default=180, description="资料和导出文件的保留天数；0 表示不自动删除")
    ignore_words: list[str] = Field(default_factory=list, description="包含这些词的消息不收集")
    ignore_regex: list[str] = Field(default_factory=list, description="匹配这些正则的消息不收集")


class DistillerConfig(PluginConfigBase):
    plugin: PluginSettings = Field(default_factory=PluginSettings)
    collector: CollectorSettings = Field(default_factory=CollectorSettings)


class MemberDistiller(MaiBotPlugin):
    config_model = DistillerConfig

    def __init__(self) -> None:
        super().__init__()
        self._store: DistillStore | None = None
        self._data_dir: Path | None = None
        self._bot_qq = ""
        self._cleanup_task: asyncio.Task[None] | None = None
        # 命令和采集共用锁，停止或删除完成后，先前排队的写入不会重新写入已删除数据。
        self._lock = asyncio.Lock()
        # 导出不占用采集锁；删除需等待导出结束，以免删除后又生成含已删资料的文件。
        # 需要两把锁时总是先取导出锁。
        self._export_lock = asyncio.Lock()

    async def on_load(self) -> None:
        self._data_dir = Path(self.ctx.paths.data_dir)
        self._store = await asyncio.to_thread(DistillStore, self._data_dir / "member_distiller.sqlite3")
        await self._run_maintenance()
        self._cleanup_task = asyncio.create_task(self._prune_context_periodically())
        self.ctx.logger.info("群友蒸馏资料插件已加载")

    async def _prune_context_periodically(self) -> None:
        ticks = 0
        while True:
            await asyncio.sleep(60)
            ticks += 1
            try:
                async with self._lock:
                    if self._store is not None:
                        await asyncio.to_thread(self._store.prune_context)
                if ticks % _MAINTENANCE_TICKS == 0:
                    await self._run_maintenance()
            except Exception as exc:
                self.ctx.logger.error("清理群聊上下文缓存失败: %s", exc)

    async def _run_maintenance(self) -> None:
        """刷新机器人 QQ，并删除超过保留期的资料。"""
        try:
            account = str(await self.ctx.config.get("bot.qq_account", "") or "").strip()
            self._bot_qq = "" if account == "0" else account
        except Exception as exc:
            self.ctx.logger.warning("读取机器人 QQ 失败，暂用上次结果: %s", exc)
        days = self.config.collector.retention_days
        if days <= 0 or self._data_dir is None:
            return
        try:
            async with self._export_lock, self._lock:
                if self._store is not None:
                    await asyncio.to_thread(self._store.prune_expired, days)
                    await asyncio.to_thread(_remove_exports, self._data_dir / "exports",
                                            older_than=time.time() - days * 86400)
        except Exception as exc:
            self.ctx.logger.error("清理过期资料失败: %s", exc)

    def _authorized(self, user_id: str) -> bool:
        allowed = {str(qq).strip() for qq in self.config.collector.allowed_qq
                   if _ID.fullmatch(str(qq).strip())}
        return user_id in allowed

    def _ignored(self, text: str) -> bool:
        collector = self.config.collector
        if any(word and word in text for word in collector.ignore_words):
            return True
        for pattern in collector.ignore_regex:
            try:
                if re.search(pattern, text):
                    return True
            except re.error as exc:
                self.ctx.logger.warning("忽略无效的 ignore_regex %r: %s", pattern, exc)
        return False

    async def on_unload(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)
            self._cleanup_task = None
        async with self._export_lock, self._lock:
            if self._store is not None:
                await asyncio.to_thread(self._store.clear_context)
                self._store = None

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """宿主已更新配置实例；无需自行重复写入或触发其他操作。"""
        del scope, config_data, version

    @HookHandler("chat.receive.after_process", name="member_distiller_collect",
                 description="收集目标 QQ 在各群本条消息中的文字", mode=HookMode.OBSERVE,
                 timeout_ms=10000, error_policy=ErrorPolicy.SKIP)
    async def on_receive(self, **kwargs: Any) -> None:
        message = kwargs.get("message")
        store = self._store
        if not self.config.plugin.enabled or store is None:
            return
        parts = _message_parts(message)
        if parts is None:
            return
        group_id, user_id, message_id = parts
        text = _actual_text(message)
        if (not store.has_targets or not text or text.startswith("/")
                or message.get("is_notify") or message.get("is_command")
                or user_id == self._bot_qq or self._ignored(text)):
            return
        # 接收时间是主程序记录的时间，不用处理时间补造，以免收进启动前积压的消息。
        received_at = message.get("received_at")
        timestamp = message.get("timestamp")
        try:
            if not isinstance(received_at, str) or not received_at or timestamp is None:
                raise ValueError("缺少消息接收时间或上游时间")
            received_seconds = datetime.fromisoformat(received_at).timestamp()
            async with self._lock:
                if self._store is store and store.has_targets:
                    await asyncio.to_thread(store.observe, group_id, user_id, message_id, text,
                                            timestamp, received_at=received_seconds)
        except Exception as exc:
            self.ctx.logger.error(f"群友资料收集失败，群 {group_id}，QQ {user_id}: {exc}")

    @HookHandler("chat.command.before_execute", name="member_distiller_forget_command",
                 description="撤销对已注册命令消息的收集", mode=HookMode.OBSERVE,
                 timeout_ms=10000, error_policy=ErrorPolicy.SKIP)
    async def on_command_matched(self, **kwargs: Any) -> None:
        # 接收 Hook 触发时宿主还没有识别命令，非 / 开头的命令要到这里才能确认。
        store = self._store
        parts = _message_parts(kwargs.get("message"))
        if store is None or parts is None or not store.has_targets:
            return
        group_id, _, message_id = parts
        try:
            async with self._lock:
                if self._store is store:
                    await asyncio.to_thread(store.forget_message, group_id, message_id)
        except Exception as exc:
            self.ctx.logger.error(f"撤销命令消息失败，群 {group_id}: {exc}")

    async def _self_service(self, store: DistillStore, action: str, user_id: str) -> str:
        """本人退出或恢复，不要求管理权限；只作用于发送者自己的 QQ。"""
        assert self._data_dir is not None
        if action == "optin":
            async with self._lock:
                await asyncio.to_thread(store.opt_in, user_id)
            return "已取消退出。管理员之后可以重新开始收集你的发言。"
        async with self._export_lock, self._lock:
            removed = await asyncio.to_thread(store.opt_out, user_id)
            await asyncio.to_thread(_remove_exports, self._data_dir / "exports", qq=user_id)
        return f"已退出收集，并删除你的 {removed} 条记录和相关导出文件。之后不会再收集你的发言。"

    @Command("member_distiller", description="管理指定 QQ 在各群的蒸馏资料", pattern=_COMMAND_PATTERN)
    async def command(self, stream_id: str = "", platform: str = "", user_id: str = "",
                      group_id: str = "", matched_groups: dict[str, Any] | None = None,
                      **kwargs: Any) -> tuple[bool, str, bool]:
        del matched_groups
        message = kwargs.get("message")
        parts = _command_parts(message)
        text = _actual_text(message)
        match = _COMMAND.fullmatch(text)
        if (not self.config.plugin.enabled or self._store is None or not stream_id or parts is None
                or platform != "qq" or parts[0] != str(group_id) or parts[1] != str(user_id)
                or match is None):
            return False, "消息来源或命令无效", False
        action = (match.group("action") or "").lower()
        qq = match.group("qq") or ""
        store = self._store
        if action in _SELF_ACTIONS and not qq:
            try:
                reply = await self._self_service(store, action, user_id)
            except Exception as exc:
                self.ctx.logger.error(f"群友资料命令失败 ({action}): {exc}")
                await self.ctx.send.text("操作失败，请联系机器人管理员。", stream_id)
                return False, "群友资料操作失败", True
            sent = await self.ctx.send.text(reply, stream_id)
            return (True, "操作完成", True) if sent else (False, "操作完成但发送回复失败", True)
        if not self._authorized(user_id):
            return False, "未授权", False
        if not action or action in _SELF_ACTIONS or (action != "list" and not qq):
            await self.ctx.send.text("用法：/distill add|stop|status|export|delete QQ号，或 /distill list\n"
                                     "收集目标 QQ 在所有可接收群聊的今后文字；delete 会停止并删除记录和导出文件。\n"
                                     "任何人都可发送 /distill optout 退出收集并删除自己的记录，/distill optin 取消退出。",
                                     stream_id)
            return True, "已发送用法", True
        if action in {"export", "list"} and group_id:
            await self.ctx.send.text("为避免在群聊暴露本机路径或收集名单，请在私聊中发送此命令。", stream_id)
            return False, "请在私聊操作", True
        try:
            reply = await self._admin_action(store, action, qq)
        except Exception as exc:
            self.ctx.logger.error(f"群友资料命令失败 ({action}): {exc}")
            await self.ctx.send.text("操作失败，请查看机器人日志。", stream_id)
            return False, "群友资料操作失败", True
        sent = await self.ctx.send.text(reply, stream_id)
        return (True, "操作完成", True) if sent else (False, "操作完成但发送回复失败", True)

    async def _admin_action(self, store: DistillStore, action: str, qq: str) -> str:
        assert self._data_dir is not None
        exports = self._data_dir / "exports"
        if action == "export":
            filename = f"qq_{qq}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}.txt"
            path = exports / filename
            async with self._export_lock:
                count = await asyncio.to_thread(store.export_text, qq, path)
            return f"已导出 {count} 条原话到机器人本机：{path}\n可直接提供给 AI 分析；该文件是原始资料，不会自动训练模型。"
        if action == "delete":
            async with self._export_lock, self._lock:
                removed = await asyncio.to_thread(store.delete_target, qq)
                files = await asyncio.to_thread(_remove_exports, exports, qq=qq)
            return f"已停止收集并删除 QQ {qq} 在所有群聊中的记录（{removed} 条）和导出文件（{files} 个）。"
        async with self._lock:
            if action == "list":
                targets = await asyncio.to_thread(store.list_targets)
                if not targets:
                    return "当前没有收集目标。"
                return "收集目标：\n" + "\n".join(
                    f"QQ {item['user_id']}：{'收集中' if item['enabled'] else '已停止'}，{item['count']} 条"
                    for item in targets)
            if action == "add":
                if await asyncio.to_thread(store.is_opted_out, qq):
                    return f"QQ {qq} 已主动退出收集，不能添加。"
                if not await asyncio.to_thread(store.set_target, qq, True):
                    return f"QQ {qq} 已在收集中，保持原有开始时间。"
                return f"已开始收集 QQ {qq} 在机器人可接收的所有群聊中此后发送的文字。"
            if action == "stop":
                await asyncio.to_thread(store.set_target, qq, False)
                return f"已停止收集 QQ {qq} 在所有群聊中的发言；已有资料保留。"
            active = await asyncio.to_thread(store.is_enabled, qq)
            count = await asyncio.to_thread(store.count, qq)
            return f"QQ {qq}（所有群聊）：{'收集中' if active else '未收集'}，已收集 {count} 条文字。"


def create_plugin() -> MemberDistiller:
    return MemberDistiller()
