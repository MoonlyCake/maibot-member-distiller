"""Tests exercise the installed SDK and the host's serialized message contract."""
from datetime import datetime
import asyncio
import os
import logging
from pathlib import Path
import re
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from maibot_member_distiller.plugin import MemberDistiller


def message(user="200", group="100", mid="1", text="hello", *, platform="qq", segments=None):
    # src/plugin_runtime/host/message_utils.py serializes timestamps as Unix-second strings.
    return {
        "platform": platform,
        "message_id": mid,
        "timestamp": str(time.time()),
        "received_at": datetime.now().isoformat(timespec="microseconds"),
        "session_id": "session-100",
        "message_info": {"platform": platform, "user_info": {"user_id": user},
                         "group_info": {"group_id": group} if group is not None else None},
        "raw_message": segments if segments is not None else [{"type": "text", "data": text}],
        "processed_plain_text": text,
        "is_command": False,
        "is_notify": False,
    }


class MetadataTest(TestCase):
    def test_sdk_registration(self):
        components = MemberDistiller().get_components()
        hooks = [item for item in components if item["type"] == "HOOK_HANDLER"]
        observer = next(item for item in hooks if item["metadata"]["mode"] == "observe")
        cmd = next(item for item in components if item["type"] == "COMMAND")
        self.assertEqual(len(hooks), 1)
        self.assertEqual(observer["metadata"]["hook"], "chat.receive.after_process")
        pattern = cmd["metadata"]["command_pattern"]
        self.assertTrue(re.fullmatch(pattern, "/distill add 200"))
        self.assertFalse(re.search(pattern, "\u2063member-distiller:/distill add 200"))
        self.assertFalse(re.search(pattern, "引用 /distill add 200"))


class PluginTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.send = AsyncMock(return_value=True)
        self.ctx = SimpleNamespace(paths=SimpleNamespace(data_dir=self.tmp.name),
                                   send=SimpleNamespace(text=self.send),
                                   config=SimpleNamespace(get_plugin=AsyncMock(return_value={})),
                                   logger=logging.getLogger("test-distill"))
        self.plugin = await self.new_plugin()

    async def asyncTearDown(self):
        await self.plugin.on_unload()
        self.tmp.cleanup()

    async def new_plugin(self):
        plugin = MemberDistiller()
        plugin._set_context(self.ctx)
        plugin.set_plugin_config({"plugin": {"enabled": True, "config_version": "1.0.0"},
                                  "collector": {"allowed_qq": ["900"]}})
        await plugin.on_load()
        return plugin

    async def command(self, text, user="900", group="100", **kwargs):
        msg = message(user=user, group=group, text=text)
        return await self.plugin.command(stream_id="session-100", platform=kwargs.get("platform", "qq"),
                                         user_id=kwargs.get("user_id", user), group_id=kwargs.get("group_id", group or ""),
                                         message=msg)

    async def test_global_collection_management_and_export(self):
        self.assertFalse((await self.command("/distill add 200", user="901"))[0])
        self.assertFalse((await self.command("/distill add 200", user="901", group=None))[0])
        self.assertFalse(self.plugin._store.is_enabled("200"))
        # A private command enables collection in all groups, not private chat.
        self.assertTrue((await self.command("/distill add 200", group=None))[0])
        await self.plugin.on_receive(message=message(text="第一句"))
        await self.plugin.on_receive(message=message(text="duplicate"))
        await self.plugin.on_receive(message=message(group="101", mid="1", text="另一群同消息ID"))
        await self.plugin.on_receive(message=message(user="201", mid="2", text="other user"))
        await self.plugin.on_receive(message=message(group=None, mid="4", text="private"))
        await self.plugin.on_receive(message=message(platform="other", mid="5", text="other platform"))
        await self.plugin.on_receive(message=message(mid="6", text="/another command"))
        await self.plugin.on_receive(message=message(mid="7", text="quoted from another person", segments=[
            {"type": "reply", "data": {"target_message_content": "do not include"}},
            {"type": "text", "data": "我自己的回答"}, {"type": "image", "data": "image content"},
        ]))
        await self.plugin.on_receive(message=message(mid="8", text="media", segments=[{"type": "image", "data": "image content"}]))
        self.assertEqual(self.plugin._store.count("200"), 3)
        # Other selected targets remain separate from 200's export and deletion.
        self.assertTrue((await self.command("/distill add 201", group="101"))[0])
        await self.plugin.on_receive(message=message(user="201", group="102", mid="9", text="另一目标"))
        self.assertTrue((await self.command("/distill status 200", group="101"))[0])
        self.assertIn("3 条", self.send.await_args.args[0])
        self.assertTrue((await self.command("/distill export 200", group=None))[0])
        exports = list((Path(self.tmp.name) / "exports").glob("*.txt"))
        self.assertEqual(len(exports), 1)
        self.assertEqual(exports[0].parent, Path(self.tmp.name) / "exports")
        self.assertEqual(os.stat(exports[0]).st_mode & 0o777, 0o600)
        content = exports[0].read_text(encoding="utf-8")
        self.assertIn("目标 QQ：200", content)
        for value in ("第一句", "另一群同消息ID", "我自己的回答", "来源群：100", "来源群：101"):
            self.assertIn(value, content)
        self.assertIn("[上下文：其他群成员，匿名", content)
        self.assertIn("other user", content)
        for other in ("duplicate", "private", "other platform", "another command",
                      "do not include", "image content", "另一目标"):
            self.assertNotIn(other, content)
        self.assertTrue((await self.command("/distill stop 200", group="101"))[0])
        await self.plugin.on_receive(message=message(group="102", mid="10", text="after stop"))
        self.assertEqual(self.plugin._store.count("200"), 3)
        await self.plugin.on_unload()
        self.plugin = await self.new_plugin()
        self.assertFalse(self.plugin._store.is_enabled("200"))
        self.assertTrue(self.plugin._store.is_enabled("201"))
        self.assertEqual(self.plugin._store.count("200"), 3)
        self.assertTrue((await self.command("/distill add 200", group=None))[0])
        self.assertTrue((await self.command("/distill delete 200", group="102"))[0])
        await self.plugin.on_receive(message=message(group="100", mid="11", text="after delete"))
        self.assertEqual(self.plugin._store.count("200"), 0)
        self.assertFalse(self.plugin._store.is_enabled("200"))
        self.assertEqual(self.plugin._store.count("201"), 1)
        self.assertTrue(self.plugin._store.is_enabled("201"))
        self.assertTrue(exports[0].exists())

    async def test_no_default_owner_and_configured_admins_only(self):
        await self.plugin.on_unload()
        plugin = MemberDistiller()
        plugin._set_context(self.ctx)
        plugin.set_plugin_config({"plugin": {"enabled": True, "config_version": "1.0.0"}})
        await plugin.on_load()
        self.plugin = plugin
        self.assertEqual(self.plugin.config.collector.allowed_qq, [])

        # Another plugin's owner is not an implicit authority for this plugin.
        self.ctx.config.get_plugin.return_value = {"image": {"owner_qq": "900"}}
        self.assertFalse((await self.command("/distill add 200", user="900", group=None))[0])
        self.assertFalse((await self.command("/distill add 201", user="901", group=None))[0])
        self.assertFalse(self.plugin._store.is_enabled("200"))
        self.assertFalse(self.plugin._store.is_enabled("201"))
        self.ctx.config.get_plugin.assert_not_awaited()

        plugin.set_plugin_config({"plugin": {"enabled": True, "config_version": "1.0.0"},
                                  "collector": {"allowed_qq": ["901", "qq:900", ""]}})
        self.assertTrue((await self.command("/distill add 202", user="901", group=None))[0])
        self.assertFalse((await self.command("/distill add 203", user="900", group=None))[0])

        plugin.set_plugin_config({"plugin": {"enabled": True, "config_version": "1.0.0"},
                                  "collector": {"allowed_qq": ["902"]}})
        self.assertFalse((await self.command("/distill add 203", user="901", group=None))[0])
        self.assertTrue((await self.command("/distill add 204", user="902", group=None))[0])
        self.assertFalse((await self.command("/distill add 205", user="900", group=None))[0])
        self.ctx.config.get_plugin.assert_not_awaited()

    async def test_empty_and_invalid_allowlist_denies(self):
        await self.plugin.on_unload()
        plugin = MemberDistiller()
        plugin._set_context(self.ctx)
        plugin.set_plugin_config({"plugin": {"enabled": True, "config_version": "1.0.0"}})
        await plugin.on_load()
        self.plugin = plugin
        for allowed in ([], ["qq:900"], [""], [" "]):
            plugin.set_plugin_config({"plugin": {"enabled": True, "config_version": "1.0.0"},
                                      "collector": {"allowed_qq": allowed}})
            self.assertFalse((await self.command("/distill add 200", user="900", group=None))[0])
        self.ctx.config.get_plugin.assert_not_awaited()
        self.assertFalse(self.plugin._store.is_enabled("200"))

    async def test_forged_source_and_quoted_command_do_not_execute(self):
        self.assertFalse((await self.command("/distill add 200", user_id="901"))[0])
        self.assertFalse((await self.command("/distill add 200", group_id="101"))[0])
        quoted = message(user="900", text="/distill add 200", segments=[
            {"type": "reply", "data": {"target_message_content": "/distill add 200"}},
            {"type": "text", "data": "普通聊天"},
        ])
        quoted["processed_plain_text"] = "/distill add 200"
        result = await self.plugin.command(stream_id="session-100", platform="qq", user_id="900",
                                           group_id="100", message=quoted)
        self.assertEqual(result[::2], (False, False))
        self.assertEqual(quoted["processed_plain_text"], "/distill add 200")
        self.assertEqual(quoted["raw_message"][1]["data"], "普通聊天")
        self.assertFalse(self.plugin._store.is_enabled("200"))

    async def test_plain_command_is_not_rewritten_and_only_authorized_user_intercepts(self):
        self.assertFalse((await self.command("/distill add 200", user="901", group=None))[2])
        msg = message(user="900", group=None, text="/distill add 200")
        original = msg.copy()
        result = await self.plugin.command(stream_id="session-100", platform="qq", user_id="900",
                                           group_id="", message=msg)
        self.assertTrue(result[0])
        self.assertTrue(result[2])
        self.assertEqual(msg, original)

    async def test_context_scoped_limited_anonymous_and_private_export(self):
        await self.plugin.on_receive(message=message(user="301", mid="pre", text="启用之前"))
        self.assertTrue((await self.command("/distill add 200", group=None))[0])
        await self.plugin.on_receive(message=message(user="301", mid="a", text="较早内容"))
        await self.plugin.on_receive(message=message(user="302", mid="b", text="上下文甲"))
        await self.plugin.on_receive(message=message(user="303", mid="c", text="上下文乙"))
        await self.plugin.on_receive(message=message(user="304", group="101", mid="d", text="另一群上下文"))
        await self.plugin.on_receive(message=message(user="305", group=None, mid="p", text="私聊内容"))
        await self.plugin.on_receive(message=message(user="306", mid="cmd", text="/命令"))
        await self.plugin.on_receive(message=message(user="200", mid="target", text="目标回答"))
        self.assertEqual(self.plugin._store.count("200"), 1)

        self.assertFalse((await self.command("/distill export 200", group="100"))[0])
        self.assertIn("私聊", self.send.await_args.args[0])
        self.assertFalse((Path(self.tmp.name) / "exports").exists())
        self.assertTrue((await self.command("/distill export 200", group=None))[0])
        exported = next((Path(self.tmp.name) / "exports").glob("*.txt")).read_text(encoding="utf-8")
        self.assertIn("[上下文：其他群成员，匿名", exported)
        self.assertIn("上下文甲", exported)
        self.assertIn("上下文乙", exported)
        self.assertIn("[目标发言]\n目标回答", exported)
        for excluded in ("启用之前", "较早内容", "另一群上下文", "私聊内容", "/命令", "302", "303"):
            self.assertNotIn(excluded, exported)

        # Stopping and restarting must not retain old context for this target.
        self.assertTrue((await self.command("/distill stop 200", group=None))[0])
        self.assertTrue((await self.command("/distill add 200", group=None))[0])
        await self.plugin.on_receive(message=message(user="200", mid="fresh", text="重新开始"))
        self.assertTrue((await self.command("/distill export 200", group=None))[0])
        newest = max((Path(self.tmp.name) / "exports").glob("*.txt"), key=lambda p: p.stat().st_mtime_ns)
        tail = newest.read_text(encoding="utf-8").split("--- 发言 2", 1)[1]
        self.assertNotIn("上下文甲", tail)
        self.assertNotIn("上下文乙", tail)

    async def test_config_update_hook_has_no_side_effects(self):
        await self.plugin.on_config_update("self", {"collector": {"allowed_qq": ["901"]}}, "1.0")
        self.assertEqual(self.plugin.config.collector.allowed_qq, ["900"])

    async def test_unload_cancels_cleanup_and_discards_context(self):
        self.assertTrue((await self.command("/distill add 200", group=None))[0])
        await self.plugin.on_receive(message=message(user="301", mid="c", text="背景"))
        store = self.plugin._store
        cleanup_task = self.plugin._cleanup_task
        self.assertTrue(store._context)
        await self.plugin.on_unload()
        self.assertTrue(cleanup_task.done())
        self.assertFalse(store._context)
        self.assertIsNone(self.plugin._store)

    async def test_cleanup_task_prunes_without_new_messages(self):
        self.plugin._cleanup_task.cancel()
        await asyncio.gather(self.plugin._cleanup_task, return_exceptions=True)
        self.plugin._cleanup_task = None
        self.plugin._store.prune_context = MagicMock()
        with patch("maibot_member_distiller.plugin.asyncio.sleep", new_callable=AsyncMock,
                   side_effect=[None, asyncio.CancelledError()]):
            with self.assertRaises(asyncio.CancelledError):
                await self.plugin._prune_context_periodically()
        self.plugin._store.prune_context.assert_called_once_with()
