"""One UTF-8 JSON request/response per connection, newline delimited."""
import asyncio
import json
import logging
import math
import os
import stat
from pathlib import Path

from .core import BridgeError

MAX_FRAME = 1_048_576
log = logging.getLogger(__name__)


async def dispatch(hub, request):
    if not isinstance(request, dict):
        raise BridgeError("request must be an object")
    cmd = request.get("command")
    allowed = {"wait": {"command", "agent", "timeout"},
               "ack": {"command", "agent", "id"},
               "working": {"command", "agent", "id", "eta"},
               "say": {"command", "agent", "reply_to", "content", "part", "final"}}
    if not isinstance(cmd, str) or cmd not in allowed:
        raise BridgeError("unknown command")
    if set(request) - allowed[cmd]:
        raise BridgeError("unknown request fields")
    agent = request.get("agent")
    if not isinstance(agent, str):
        raise BridgeError("agent is required")
    hub._agent(agent)
    if cmd == "wait":
        timeout = request.get("timeout", 540)
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and not math.isfinite(timeout):
            raise BridgeError("timeout must be finite")
        return await hub.wait(agent, timeout)
    if cmd == "ack":
        return await hub.ack(agent, request.get("id"))
    if cmd == "working":
        return await hub.working(agent, request.get("id"), request.get("eta"))
    return await hub.say(agent, request.get("reply_to"), request.get("content"),
                         request.get("part"), request.get("final", False))


async def handle_connection(hub, reader, writer):
    operation = None
    disconnect = None
    try:
        line = await asyncio.wait_for(reader.readline(), 10)
        if not line.endswith(b"\n") or len(line) > MAX_FRAME:
            raise BridgeError("request must be a newline-terminated JSON frame <= 1 MiB")
        request = json.loads(line)
        operation = asyncio.create_task(dispatch(hub, request))
        # A vanished wait must not leave a phantom waiter (and infinite typing).
        # say/ack must finish even if the caller disappears after sending.
        if isinstance(request, dict) and request.get("command") == "wait":
            disconnect = asyncio.create_task(reader.read(1))
            done, _ = await asyncio.wait((operation, disconnect), return_when=asyncio.FIRST_COMPLETED)
            if operation not in done:
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
                writer.close()
                await writer.wait_closed()
                return
        response = await operation
    except (BridgeError, ValueError, UnicodeError, asyncio.TimeoutError) as exc:
        response = {"error": {"code": "bad_request", "message": str(exc)}}
    except asyncio.CancelledError:
        if operation:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        raise
    except Exception as exc:
        log.error("socket operation failed (%s)", type(exc).__name__)
        response = {"error": {"code": "operation_failed", "message": "Operation failed; retry safely by id/part"}}
    finally:
        if disconnect:
            disconnect.cancel()
            await asyncio.gather(disconnect, return_exceptions=True)
    try:
        writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()
    except (ConnectionError, BrokenPipeError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass


class SocketServer:
    def __init__(self, hub):
        self.hub, self.server = hub, None
        self.connections = set()
        self.path = Path(hub.config.socket_path)
        self.identity = None

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        try:
            await handle_connection(self.hub, reader, writer)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.connections.discard(task)

    async def start(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() or self.path.is_symlink():
            original = self.path.lstat()
            if not stat.S_ISSOCK(original.st_mode):
                raise BridgeError("Refusing to replace a non-socket path")
            try:
                _, probe = await asyncio.open_unix_connection(str(self.path))
            except ConnectionRefusedError:
                current = self.path.lstat()
                if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                    raise BridgeError("Socket path changed during stale-socket check")
                self.path.unlink()
            else:
                probe.close()
                await probe.wait_closed()
                raise BridgeError("Another server is listening on the socket")
        self.server = await asyncio.start_unix_server(self.handle, str(self.path), limit=MAX_FRAME + 1)
        os.chmod(self.path, 0o660)
        socket_stat = self.path.stat()
        self.identity = (socket_stat.st_dev, socket_stat.st_ino)
        self.hub.socket_listening = True

    async def close(self):
        self.hub.socket_listening = False
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for task in list(self.connections):
            task.cancel()
        await asyncio.gather(*list(self.connections), return_exceptions=True)
        if self.identity and self.path.exists():
            stat = self.path.stat()
            if self.identity == (stat.st_dev, stat.st_ino):
                self.path.unlink()
