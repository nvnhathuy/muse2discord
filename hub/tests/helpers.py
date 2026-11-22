import asyncio
from dataclasses import replace

from bridgehub.config import AGENTS, Agent, Config
from bridgehub.core import Message


def config(path):
    return Config(10, 20, 1, {
        "lead": Agent("lead", 101, "offline-placeholder", "Lead", ("lead", "lead")),
        "worker": Agent("worker", 102, "offline-placeholder", "Worker", ("worker",)),
        "deputy": Agent("deputy", 103, "offline-placeholder", "Deputy", ("deputy",)),
    }, str(path / "journal.jsonl"), str(path / "bridge.sock"))


def msg(mid=1000, text='"lead điểm danh', author=1, **kwargs):
    return replace(Message(str(mid), 10, 20, author, text), **kwargs)


async def empty_history(agent, after):
    if False:
        yield None


class FakeTransport:
    def __init__(self):
        self.sent, self.reactions, self.typings = [], [], []
        self.fail_send = False
        self.block_react = None
        self.warnings = []
        self.fail_warning = False
        self.connected = True

    def healthy(self):
        return self.connected

    async def send_warning(self, content, edit_id=None, mention=False):
        if self.fail_warning:
            raise RuntimeError("fake warning failure")
        mid = edit_id or str(99000 + len(self.warnings))
        self.warnings.append((content, edit_id, mention, mid))
        return mid

    async def send_reply(self, agent, reply_to, content, edit_id=None):
        if self.fail_send:
            raise RuntimeError("fake send failure")
        message_id = edit_id or str(90000 + len(self.sent))
        self.sent.append((agent, reply_to, content, edit_id, message_id))
        return message_id

    async def react(self, agent, mid):
        self.reactions.append((agent, mid))
        if self.block_react:
            await self.block_react.wait()

    async def typing(self, agent):
        self.typings.append(agent)


async def eventually(predicate, timeout=1):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.001)
