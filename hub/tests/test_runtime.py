import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from bridgehub.core import Hub
from bridgehub.discord_runtime import DiscordRuntime
from .helpers import config, eventually


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_adapter_typing_reaction_reply_edit_and_history_arguments(self):
        with tempfile.TemporaryDirectory() as path:
            runtime = DiscordRuntime(config(Path(path)))
            channel = SimpleNamespace(typing=AsyncMock(), send=AsyncMock(return_value=SimpleNamespace(id=9000)))
            partial = SimpleNamespace(add_reaction=AsyncMock(), edit=AsyncMock(return_value=SimpleNamespace(id=9000)))
            channel.get_partial_message = lambda mid: partial
            runtime.channels["worker"] = channel
            runtime.online["worker"].set()
            await runtime.typing("worker")
            await runtime.react("worker", "1000")
            self.assertEqual(await runtime.send_reply("worker", "1000", "first"), "9000")
            self.assertEqual(await runtime.send_reply("worker", "1000", "edit", "9000"), "9000")
            channel.typing.assert_awaited_once()
            partial.add_reaction.assert_awaited_once_with("👍")
            self.assertIs(channel.send.call_args.kwargs["mention_author"], False)
            self.assertIsInstance(channel.send.call_args.kwargs["allowed_mentions"], discord.AllowedMentions)
            partial.edit.assert_awaited_once()
            calls = []
            async def history(**kwargs):
                calls.append(kwargs)
                if False:
                    yield None
            channel.history = history
            self.assertEqual([m async for m in runtime.history("worker", 123)], [])
            self.assertEqual(calls[0]["after"].id, 123)
            self.assertTrue(calls[0]["oldest_first"])
            self.assertIsNone(calls[0]["limit"])
            self.assertTrue(callable(discord.TextChannel.typing))

    async def test_one_client_failure_restarts_only_that_client_in_same_loop(self):
        with tempfile.TemporaryDirectory() as path:
            cfg = config(Path(path))
            instances = {a: [] for a in cfg.agents}
            loop_ids = set()
            # Unique fake placeholders let the fake login select its agent.
            from dataclasses import replace
            cfg = replace(cfg, agents={a: replace(c, token=f"offline-{a}") for a, c in cfg.agents.items()})
            class FakeClient:
                def __init__(self, intents):
                    self.closed = False
                    self.stop = asyncio.Event()
                def event(self, fn):
                    setattr(self, fn.__name__, fn)
                    return fn
                async def start(self, token, reconnect):
                    agent = token.removeprefix("offline-")
                    instances[agent].append(self)
                    loop_ids.add(id(asyncio.get_running_loop()))
                    if agent == "worker" and len(instances[agent]) == 1:
                        raise RuntimeError("isolated fake failure")
                    self.user = SimpleNamespace(id=cfg.agents[agent].bot_id)
                    await self.on_ready()
                    await self.stop.wait()
                def get_channel(self, channel_id):
                    return SimpleNamespace(guild=SimpleNamespace(id=cfg.guild_id))
                async def close(self):
                    self.closed = True
                    self.stop.set()
            module = SimpleNamespace(Client=FakeClient, Intents=discord.Intents)
            runtime = DiscordRuntime(cfg, module, retry_base=.005, retry_max=.01)
            runtime.history = lambda agent, after: self.empty_history()
            hub = Hub(cfg, runtime)
            runtime.hub = hub
            task = asyncio.create_task(runtime.run())
            try:
                await eventually(lambda: hub.ready.is_set())
                self.assertEqual({a: len(v) for a, v in instances.items()}, {"lead": 1, "worker": 2, "deputy": 1})
                self.assertEqual(len(loop_ids), 1)
                self.assertFalse(instances["lead"][0].closed)
                self.assertFalse(instances["deputy"][0].closed)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await hub.close()
            self.assertTrue(all(c.closed for group in instances.values() for c in group))

    @staticmethod
    async def empty_history():
        if False:
            yield None
