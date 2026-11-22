import asyncio
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bridgehub.core import BridgeError, Hub
from bridgehub.protocol import MAX_FRAME, SocketServer
from .helpers import FakeTransport, config, empty_history, eventually, msg

HAS_UNIX = hasattr(socket, "AF_UNIX") and hasattr(asyncio, "start_unix_server")


class SocketPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_regular_file_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge.sock"
            path.write_text("keep this file", encoding="utf-8")
            server = SocketServer(SimpleNamespace(config=SimpleNamespace(socket_path=str(path))))
            with self.assertRaisesRegex(BridgeError, "non-socket path"):
                await server.start()
            self.assertEqual(path.read_text(encoding="utf-8"), "keep this file")
            self.assertIsNone(server.server)


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.transport = FakeTransport()
        self.hub = Hub(config(Path(self.temp.name)), self.transport)
        await self.hub.recover(empty_history)
        self.socket_server = SocketServer(self.hub)
        if HAS_UNIX:
            await self.socket_server.start()
            self.server = self.socket_server.server
        else:
            # Exactly the production connection handler and framing; TCP only
            # substitutes for the unavailable Windows AF_UNIX transport.
            self.server = await asyncio.start_server(self.socket_server.handle, "127.0.0.1", 0,
                                                     limit=MAX_FRAME + 1)
            self.socket_server.server = self.server

    async def asyncTearDown(self):
        await self.socket_server.close()
        await self.hub.close()
        self.temp.cleanup()

    async def connect(self):
        if HAS_UNIX:
            return await asyncio.open_unix_connection(self.hub.config.socket_path)
        port = self.server.sockets[0].getsockname()[1]
        return await asyncio.open_connection("127.0.0.1", port)

    async def request(self, request=None, raw=None):
        reader, writer = await self.connect()
        writer.write(raw if raw is not None else (json.dumps(request) + "\n").encode())
        await writer.drain()
        data = await asyncio.wait_for(reader.readline(), 2)
        self.assertTrue(data.endswith(b"\n"))
        result = json.loads(data)
        self.assertEqual(await reader.read(), b"")
        writer.close()
        await writer.wait_closed()
        return result

    async def test_fake_client_wait_say_ack_json_roundtrip(self):
        self.assertEqual(await self.request({"command": "wait", "agent": "lead", "timeout": .01}),
                         {"messages": [], "context": [], "heartbeat": True})
        waiting = asyncio.create_task(self.request({"command": "wait", "agent": "lead", "timeout": 1}))
        await eventually(lambda: self.hub.waiters["lead"] == 1)
        await self.hub.ingest(msg(1000))
        result = await waiting
        self.assertEqual(result["messages"][0]["id"], "1000")
        self.assertIsInstance(result["context"], list)
        response = await self.request({"command": "say", "agent": "lead", "reply_to": "1000",
                                       "content": "báo cáo", "part": 1, "final": True})
        self.assertTrue(response["ok"])
        self.assertEqual(response["last_acked_id"], "1000")
        self.assertEqual(self.transport.sent[0][0], "lead")
        await self.hub.ingest(msg(1001))
        ack = await self.request({"command": "ack", "agent": "lead", "id": "1001"})
        self.assertEqual(ack, {"ok": True, "last_acked_id": "1001"})

    async def test_disconnect_cancels_waiter(self):
        reader, writer = await self.connect()
        writer.write(b'{"command":"wait","agent":"lead","timeout":540}\n')
        await writer.drain()
        await eventually(lambda: self.hub.waiters["lead"] == 1)
        writer.close()
        await writer.wait_closed()
        await eventually(lambda: self.hub.waiters["lead"] == 0)

    async def test_invalid_requests_return_json_errors(self):
        bad = [b'not json\n', b'[]\n', b'{"command":"delete","agent":"lead"}\n',
               b'{"command":"wait","agent":"stranger"}\n',
               b'{"command":"wait","agent":"lead","timeout":NaN}\n',
               b'{"command":"wait","agent":"lead","timeout":-1}\n',
               b'{"command":"wait","agent":"lead","timeout":true}\n',
               b'{"command":"wait","agent":"lead","extra":1}\n',
               b'{"command":"ack","agent":"lead","id":"9999"}\n',
               b'{"command":"say","agent":"lead","reply_to":"1000","content":""}\n',
               b'\xff\n']
        for raw in bad:
            with self.subTest(raw=raw):
                self.assertEqual((await self.request(raw=raw))["error"]["code"], "bad_request")

    async def test_send_failure_is_json_error_does_not_ack(self):
        await self.hub.ingest(msg())
        self.transport.fail_send = True
        result = await self.request({"command": "say", "agent": "lead", "reply_to": "1000", "content": "x"})
        self.assertEqual(result["error"]["code"], "operation_failed")
        self.assertEqual(self.hub.acked["lead"], 0)

    async def test_fragmented_request(self):
        reader, writer = await self.connect()
        writer.write(b'{"command":"wait",')
        await writer.drain()
        writer.write(b'"agent":"lead","timeout":0}\n')
        await writer.drain()
        self.assertEqual(json.loads(await reader.readline()), {"messages": [], "context": [], "heartbeat": True})
        writer.close()
        await writer.wait_closed()

    @unittest.skipUnless(HAS_UNIX, "Windows Python lacks AF_UNIX; production protocol exercised over loopback")
    async def test_native_unix_socket_and_actual_cli_subprocess(self):
        self.assertEqual(os.stat(self.hub.config.socket_path).st_mode & 0o777, 0o660)
        await self.hub.ingest(msg())
        command = [sys.executable, str(Path(__file__).parents[1] / "bridge"),
                   "--socket", self.hub.config.socket_path]
        async def cli(args, content=None):
            proc = await asyncio.create_subprocess_exec(*command, *args,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            out, err = await proc.communicate(content)
            self.assertEqual(proc.returncode, 0, err.decode())
            return json.loads(out)
        self.assertEqual((await cli(["wait", "--agent", "lead", "--timeout", "0"]))["messages"][0]["id"], "1000")
        self.assertTrue((await cli(["say", "--agent", "lead", "--reply-to", "1000"], "answer".encode()))["ok"])
        self.assertTrue((await cli(["ack", "--agent", "lead", "1000"]))["ok"])

    @unittest.skipUnless(HAS_UNIX, "Native Unix socket lifecycle requires Linux/macOS")
    async def test_stale_socket_recovery_and_live_path_protection(self):
        other = SocketServer(self.hub)
        with self.assertRaises(BridgeError):
            await other.start()
        await self.socket_server.close()
        stale = socket.socket(socket.AF_UNIX)
        stale.bind(self.hub.config.socket_path)
        stale.close()
        await self.socket_server.start()
        self.assertEqual(await self.request({"command": "wait", "agent": "lead", "timeout": 0}), {"messages": [], "context": [], "heartbeat": True})


def load_cli():
    loader = importlib.machinery.SourceFileLoader("bridge_cli", str(Path(__file__).parents[1] / "bridge"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.mark.parametrize("argv,stdin,response,expected", [
    (["wait", "--agent", "lead", "--timeout", "2"], "", {"timeout": True},
     {"command": "wait", "agent": "lead", "timeout": 2.0}),
    (["say", "--reply-to", "1000", "--part", "2", "--final"], "world", {"ok": True},
     {"command": "say", "agent": "worker", "reply_to": "1000", "part": 2, "final": True, "content": "world"}),
    (["ack", "--agent", "deputy", "1000"], "", {"ok": True},
     {"command": "ack", "agent": "deputy", "id": "1000"}),
    (["working", "--agent", "lead", "--eta", "4"], "", {"ok": True},
     {"command": "working", "agent": "lead", "id": None, "eta": 4.0}),
    (["working", "1000"], "", {"ok": True},
     {"command": "working", "agent": "worker", "id": "1000", "eta": None}),
])
def test_cli_encoding_arguments_stdin_and_json_stdout(argv, stdin, response, expected):
    module = load_cli()
    captured = []
    class FakeSocket:
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def connect(self, path): assert path == "/fake/bridge.sock"
        def sendall(self, data): captured.append(json.loads(data))
        def makefile(self, mode): return io.BytesIO((json.dumps(response) + "\n").encode())
    output = io.StringIO()
    with patch.object(module.socket, "AF_UNIX", 1, create=True), patch.object(module.socket, "socket", FakeSocket), \
            patch.object(sys, "argv", ["bridge", "--socket", "/fake/bridge.sock", *argv]), \
            patch.object(sys, "stdin", io.StringIO(stdin)), patch.dict(os.environ, {"BRIDGE_AGENT": "worker"}), \
            contextlib.redirect_stdout(output):
        assert module.main() == 0
    assert captured == [expected]
    assert json.loads(output.getvalue()) == response
