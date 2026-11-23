"""Official discord.py adapter; tests inject fake clients, never use a token."""
import asyncio
import logging

from .core import BridgeError, Message

log = logging.getLogger(__name__)


async def finish_cleanup(task):
    """Wait for cleanup to finish before propagating caller cancellation."""
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    if interrupted:
        raise asyncio.CancelledError
    return task.result()


def convert(message):
    return Message(str(message.id), message.guild.id if message.guild else 0,
                   message.channel.id, message.author.id, message.content,
                   tuple(u.id for u in message.mentions), message.author.display_name,
                   message.created_at.isoformat(), tuple(a.url for a in message.attachments))


class DiscordRuntime:
    def __init__(self, config, discord_module=None, *, retry_base=2, retry_max=60):
        if discord_module is None:
            import discord as discord_module
        self.discord = discord_module
        self.config = config
        self.hub = None
        self.clients = {}
        self.channels = {}
        # The runtime supervises exactly the EFFECTIVE agent set from the
        # config (shared-registry keys in integrated mode; the env-built
        # lead/worker/deputy set in legacy mode) - never the AGENTS
        # constant, which is only the legacy default key list.
        self.runtime_agents = tuple(config.agents) + (("hub",) if config.hub_bot_id else ())
        # Per-key supervise tasks (PATCH-POINTS section 4): run() registers
        # the initial set here; the registry connector adds/removes entries
        # as agents come and go, instead of a fixed task tuple.
        self.agent_tasks = {}
        self.online = {a: asyncio.Event() for a in self.runtime_agents}
        self.warning_ids = set()
        self.warning_texts = set()
        self.recovery_requested = asyncio.Event()
        self.retry_base, self.retry_max = retry_base, retry_max

    def _default_agent(self):
        # Fallback identity for hub-originated messages when no dedicated
        # hub bot is configured: the first EFFECTIVE agent (the "lead" key
        # in legacy mode, the first registry key in integrated mode).
        return next(iter(self.config.agents))

    def channel(self, agent):
        if not self.online[agent].is_set() or agent not in self.channels:
            raise BridgeError(f"{agent} bot is not ready; retry later")
        return self.channels[agent]

    async def typing(self, agent):
        await self.channel(agent).typing()

    async def react(self, agent, message_id):
        await self.channel(agent).get_partial_message(int(message_id)).add_reaction("👍")

    async def send_reply(self, agent, reply_to, content, edit_id=None):
        channel = self.channel(agent)
        mentions = self.discord.AllowedMentions.none()
        if edit_id:
            sent = await channel.get_partial_message(int(edit_id)).edit(content=content, allowed_mentions=mentions)
        else:
            sent = await channel.send(content, reference=channel.get_partial_message(int(reply_to)),
                                      mention_author=False, allowed_mentions=mentions)
        return str(sent.id)

    def healthy(self):
        return all(event.is_set() for event in self.online.values())

    async def send_warning(self, content, edit_id=None, mention=False):
        # A dedicated hub identity is preferred; otherwise use the default
        # (first effective) agent and exclude our own generated messages by
        # ID/content from ingestion and history.
        channel = self.channel("hub" if self.config.hub_bot_id else self._default_agent())
        allowed = (self.discord.AllowedMentions(users=[self.discord.Object(id=self.config.owner_id)],
                                               roles=False, everyone=False, replied_user=False)
                   if mention else self.discord.AllowedMentions.none())
        self.warning_texts.add(content)
        sent = None
        if edit_id:
            sent = await self._edit_warning(int(edit_id), content, allowed)
        if sent is None:
            sent = await channel.send(content, allowed_mentions=allowed)
        self.warning_ids.add(str(sent.id))
        return str(sent.id)

    async def _edit_warning(self, edit_id, content, allowed):
        # Discord lets a bot edit only its OWN messages, but a stored warning
        # may have been posted by another of our bots (warning state restored
        # after the registry order changed which agent is the default), and
        # editing it with the wrong bot is refused with Forbidden forever.
        # Try each of our bots in turn - the author's bot succeeds. Returns
        # None when no bot may edit the message, so the caller posts a fresh
        # warning and stores the new ID instead of retrying a doomed edit.
        if self.config.hub_bot_id:
            keys = ("hub",)
        else:
            default = self._default_agent()
            keys = (default,) + tuple(a for a in self.config.agents if a != default)
        for key in keys:
            try:
                channel = self.channel(key)
                return await channel.get_partial_message(edit_id).edit(
                    content=content, allowed_mentions=allowed)
            except Exception:
                continue
        return None

    def _hub_message(self, message):
        sender = self.config.hub_bot_id or self.config.agents[self._default_agent()].bot_id
        if message.author.id != sender:
            return False
        persisted = {g[key] for g in getattr(self.hub, "warning_state", [])
                     for key in ("message_id", "tier2_id") if g.get(key)}
        return (bool(self.config.hub_bot_id) or str(message.id) in self.warning_ids | persisted
                or message.content in self.warning_texts)

    async def history(self, agent, after_id):
        channel = self.channel(agent)
        async for msg in channel.history(limit=None, oldest_first=True,
                                         after=self.discord.Object(id=after_id)):
            if not self._hub_message(msg):
                yield convert(msg)

    async def supervise(self, agent):
        """Only this agent's Client is replaced when its gateway/callback fails."""
        delay = self.retry_base
        while True:
            try:
                await self._one_client(agent)
                log.warning("client ended; restarting agent=%s", agent)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("client failed; restarting agent=%s (%s)", agent, type(exc).__name__)
            await asyncio.sleep(delay)
            delay = min(self.retry_max, max(self.retry_base, delay * 2))

    async def _one_client(self, agent):
        # Each attempt has its own callback closure. A stale event from an old
        # Client must never clear readiness for its replacement.
        client = self.discord.Client(intents=self.discord.Intents(
            guilds=True, guild_messages=True, message_content=True))
        self.clients[agent] = client
        fault = asyncio.get_running_loop().create_future()
        start_task = None
        active = True

        async def on_ready():
            if not active:
                return
            try:
                expected_id = self.config.hub_bot_id if agent == "hub" else self.config.agents[agent].bot_id
                if client.user.id != expected_id:
                    raise BridgeError("Configured bot ID does not match logged-in client")
                channel = client.get_channel(self.config.channel_id)
                if channel is None:
                    channel = await client.fetch_channel(self.config.channel_id)
                if not active:
                    return
                if not channel.guild or channel.guild.id != self.config.guild_id:
                    raise BridgeError("Configured channel does not belong to configured guild")
                self.channels[agent] = channel
                self.online[agent].set()
                self.recovery_requested.set()
                log.info("bot ready agent=%s display_name=%s", agent,
                         "hub" if agent == "hub" else self.config.agents[agent].display_name)
            except Exception as exc:
                if not fault.done():
                    fault.set_result(type(exc).__name__)

        async def on_resumed():
            if active:
                self.online[agent].set()
                self.recovery_requested.set()

        async def on_disconnect():
            if active:
                self.online[agent].clear()

        async def on_message(message):
            if active and not self._hub_message(message):
                try:
                    await self.hub.ingest(convert(message))
                except Exception as exc:
                    if not fault.done():
                        fault.set_result(type(exc).__name__)

        async def on_error(event, *args, **kwargs):
            if active and not fault.done():
                fault.set_result("CallbackError")

        try:
            for callback in (on_ready, on_resumed, on_disconnect, on_message, on_error):
                client.event(callback)
            token = self.config.hub_bot_token if agent == "hub" else self.config.agents[agent].token
            start_task = asyncio.create_task(client.start(token, reconnect=True))
            done, _ = await asyncio.wait((start_task, fault), return_when=asyncio.FIRST_COMPLETED)
            if start_task in done:
                await start_task
        finally:
            active = False
            self.online[agent].clear()
            self.channels.pop(agent, None)
            await finish_cleanup(asyncio.create_task(self._close_client(agent, client, start_task, fault)))

    async def _close_client(self, agent, client, start_task, fault):
        if start_task:
            start_task.cancel()
            await asyncio.gather(start_task, return_exceptions=True)
        fault.cancel()
        try:
            await client.close()
        except Exception as exc:
            log.warning("client close failed agent=%s (%s)", agent, type(exc).__name__)

    async def recovery_loop(self):
        while True:
            await self.recovery_requested.wait()
            self.recovery_requested.clear()
            await asyncio.gather(*(event.wait() for event in self.online.values()))
            try:
                await self.hub.recover(self.history)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("history recovery failed; delivery held until retry (%s)", type(exc).__name__)
                await asyncio.sleep(self.retry_base)
                self.recovery_requested.set()

    async def run(self):
        for a in self.runtime_agents:
            if a not in self.agent_tasks or self.agent_tasks[a].done():
                self.agent_tasks[a] = asyncio.create_task(self.supervise(a))
        recovery = asyncio.create_task(self.recovery_loop())
        tasks = [*self.agent_tasks.values(), recovery]
        workers = asyncio.gather(*tasks)
        try:
            # Own cancellation here: cancelling gather directly can return as
            # soon as one child exits, before the other clients finish closing.
            await asyncio.shield(workers)
        finally:
            # Include supervise tasks the registry connector added later.
            tasks = list({*tasks, *self.agent_tasks.values()})
            for task in tasks:
                task.cancel()
            await finish_cleanup(asyncio.gather(workers, *tasks, return_exceptions=True))


class RuntimeConnector:
    """BotLifecycle connector over DiscordRuntime (PATCH-POINTS section 4).

    connect(key, token): start a fresh supervise(key) task. The token is
    NOT passed to the client directly - _one_client reads it from
    config.agents, which the registry watcher has already updated, so a
    reconnect after a token change uses the new token.
    disconnect(key): cancel that key's supervise task; the client is
    closed by supervise's own cleanup path (_close_client/finish_cleanup).
    """

    def __init__(self, runtime):
        self.runtime = runtime

    async def connect(self, key, token):
        runtime = self.runtime
        runtime.online.setdefault(key, asyncio.Event())
        task = runtime.agent_tasks.get(key)
        if task is not None and not task.done():
            return
        runtime.agent_tasks[key] = asyncio.create_task(runtime.supervise(key))

    async def disconnect(self, key):
        runtime = self.runtime
        task = runtime.agent_tasks.pop(key, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if key in runtime.online:
            runtime.online[key].clear()
        runtime.channels.pop(key, None)
