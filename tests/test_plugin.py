"""Tests exercise the installed SDK and the host's serialized message contract."""
from datetime import datetime
import os
import logging
from pathlib import Path
import re
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock

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
        hook = next(item for item in hooks if item["metadata"]["mode"] == "blocking")
        observer = next(item for item in hooks if item["metadata"]["mode"] == "observe")
        cmd = next(item for item in components if item["type"] == "COMMAND")
        self.assertEqual(hook["metadata"]["hook"], "chat.receive.after_process")
        self.assertEqual(hook["metadata"]["mode"], "blocking")
        self.assertEqual(observer["metadata"]["hook"], "chat.receive.after_process")
        self.assertTrue(re.fullmatch(cmd["metadata"]["command_pattern"], "\u2063member-distiller:/distill add 200"))


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
        routed = await self.plugin.route_command(message=msg)
        msg = routed.get("modified_kwargs", {}).get("message", msg)
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
        for other in ("duplicate", "other user", "private", "other platform", "another command",
                      "do not include", "image content", "另一目标"):
            self.assertNotIn(other, content)
        self.assertTrue((await self.command("/distill stop 200", group="101"))[0])
        await self.plugin.on_receive(message=message(group="102", mid="10", text="after stop"))
        self.assertEqual(self.plugin._store.count("200"), 3)
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

    async def test_owner_follows_image_plugin_and_extra_admins(self):
        plugin = MemberDistiller()
        plugin._set_context(self.ctx)
        plugin.set_plugin_config({"plugin": {"enabled": True, "config_version": "1.0.0"}})
        await plugin.on_load()
        self.plugin = plugin
        self.assertEqual(self.plugin.config.collector.allowed_qq, [])

        self.ctx.config.get_plugin.return_value = {"image": {"owner_qq": "900"}}
        self.assertTrue((await self.command("/distill add 200", user="900", group=None))[0])
        self.assertFalse((await self.command("/distill add 201", user="901", group=None))[0])
        self.assertTrue(self.plugin._store.is_enabled("200"))
        self.assertFalse(self.plugin._store.is_enabled("201"))
        self.ctx.config.get_plugin.assert_awaited_with("com.moonlycake.image-generation")

        # The source owner is read afresh so changing it revokes the old owner.
        self.ctx.config.get_plugin.return_value = {"image": {"owner_qq": "901"}}
        self.assertFalse((await self.command("/distill add 202", user="900", group=None))[0])
        self.assertTrue((await self.command("/distill add 202", user="901", group=None))[0])

        plugin.set_plugin_config({"plugin": {"enabled": True, "config_version": "1.0.0"},
                                  "collector": {"allowed_qq": ["902"]}})
        self.assertTrue((await self.command("/distill add 203", user="901", group=None))[0])
        self.assertTrue((await self.command("/distill add 204", user="902", group=None))[0])
        self.assertFalse((await self.command("/distill add 205", user="900", group=None))[0])

    async def test_owner_missing_or_unreadable_denies_without_extras(self):
        plugin = MemberDistiller()
        plugin._set_context(self.ctx)
        plugin.set_plugin_config({"plugin": {"enabled": True, "config_version": "1.0.0"}})
        await plugin.on_load()
        self.plugin = plugin
        for source in ({}, {"image": {}}, {"image": {"owner_qq": ""}},
                       {"image": {"owner_qq": "qq:900"}}, None):
            self.ctx.config.get_plugin.return_value = source
            self.assertFalse((await self.command("/distill add 200", user="900", group=None))[0])
        self.ctx.config.get_plugin.side_effect = RuntimeError("config unavailable")
        self.assertFalse((await self.command("/distill add 200", user="900", group=None))[0])
        self.assertFalse(self.plugin._store.is_enabled("200"))

    async def test_forged_source_and_quoted_command_do_not_execute(self):
        self.assertFalse((await self.command("/distill add 200", user_id="901"))[0])
        self.assertFalse((await self.command("/distill add 200", group_id="101"))[0])
        quoted = message(user="900", text="/distill add 200", segments=[
            {"type": "reply", "data": {"target_message_content": "/distill add 200"}},
            {"type": "text", "data": "普通聊天"},
        ])
        routed = await self.plugin.route_command(message=quoted)
        self.assertNotIn("modified_kwargs", routed)
        self.assertFalse(self.plugin._store.is_enabled("200"))
