import asyncio
import json
import logging
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from bridgehub.core import BridgeError, Hub, Journal
from .helpers import FakeTransport, config, empty_history, eventually, msg


class CoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.transport = FakeTransport()
        self.hub = Hub(config(self.path), self.transport, typing_interval=.01, typing_grace=.04)
        await self.hub.recover(empty_history)

    async def asyncTearDown(self):
        await self.hub.close()
        self.temp.cleanup()

    async def restart(self):
        await self.hub.close()
        self.hub = Hub(config(self.path), self.transport, typing_interval=.01, typing_grace=.04)
        await self.hub.recover(empty_history)

    async def test_wait_blocks_then_wakes_under_300ms_and_does_not_ack(self):
        task = asyncio.create_task(self.hub.wait("lead", 1))
        await asyncio.sleep(.02)
        self.assertFalse(task.done())
        start = time.perf_counter()
        await self.hub.ingest(msg())
        result = await task
        elapsed = time.perf_counter() - start
        print(f"WAKE_LATENCY_SECONDS={elapsed:.6f} (<0.300 required)")
        self.assertLess(elapsed, .3)
        self.assertEqual([m["id"] for m in result["messages"]], ["1000"])
        self.assertEqual(self.hub.acked["lead"], 0)
        self.assertTrue((await self.hub.wait("lead", 0))["heartbeat"])

    async def test_timeout_exact_shape_and_no_cursor_change(self):
        self.assertEqual(await self.hub.wait("lead", .01), {"messages": [], "context": [], "heartbeat": True})
        self.assertEqual(self.hub.acked["lead"], 0)

    async def test_ack_is_exact_monotone_and_agent_local(self):
        await self.hub.ingest(msg(1000, "tất cả"))
        await self.hub.ingest(msg(1001, "tất cả"))
        await self.hub.ack("lead", "1001")
        await self.hub.ack("lead", "1000")
        self.assertEqual(self.hub.acked, {"lead": 1001, "worker": 0, "deputy": 0})
        self.assertTrue((await self.hub.wait("lead", 0))["heartbeat"])
        self.assertEqual(len((await self.hub.wait("worker", 0))["messages"]), 2)

    async def test_unknown_or_wrong_agent_ack_cannot_skip_work(self):
        await self.hub.ingest(msg())
        for agent, mid in [("lead", "9999"), ("worker", "1000"), ("bad", "1000")]:
            with self.assertRaises(BridgeError):
                await self.hub.ack(agent, mid)
        self.assertEqual(self.hub.acked["lead"], 0)

    async def test_context_is_explicit_excludes_pending_and_does_not_route(self):
        await self.hub.ingest(msg(999, "room context"))
        await self.hub.ingest(msg())
        result = await self.hub.wait("lead", 0)
        self.assertEqual([m["id"] for m in result["messages"]], ["1000"])
        self.assertEqual(result["context"][0]["id"], "999")
        self.assertEqual(result["context"][0]["kind"], "context_only")
        self.assertTrue(result["context"][0]["do_not_reply"])

    async def test_same_message_from_three_clients_counts_only_once(self):
        for _ in range(3):
            await self.hub.ingest(msg(author=102))
        self.assertEqual(len(self.hub.queues["lead"]), 1)
        self.assertEqual(self.hub.filter.counts["lead"], 1)
        self.assertEqual(len(self.hub.sources), 1)

    async def test_restart_preserves_unacked_and_cursor(self):
        await self.hub.ingest(msg(1000))
        await self.hub.ingest(msg(1001))
        await self.hub.ack("lead", "1000")
        await self.restart()
        self.assertEqual(self.hub.acked["lead"], 1000)
        self.assertEqual([m["id"] for m in (await self.hub.wait("lead", 0))["messages"]], ["1001"])

    async def test_preexisting_jsonl_fixture(self):
        await self.hub.close()
        shutil.copyfile(Path(__file__).parent / "fixtures" / "replay.jsonl", self.path / "journal.jsonl")
        self.hub = Hub(config(self.path), self.transport)
        await self.hub.recover(empty_history)
        result = await self.hub.wait("lead", 0)
        self.assertEqual(result["last_acked_id"], "1000")
        self.assertEqual([m["id"] for m in result["messages"]], ["1001"])

    async def test_say_uses_agent_client_ack_only_after_success(self):
        await self.hub.ingest(msg(text='"worker'))
        self.transport.fail_send = True
        with self.assertRaises(RuntimeError):
            await self.hub.say("worker", "1000", "answer")
        self.assertEqual(self.hub.acked["worker"], 0)
        self.transport.fail_send = False
        await self.hub.say("worker", "1000", "answer")
        self.assertEqual(self.transport.sent[0][:3], ("worker", "1000", "answer"))
        self.assertEqual(self.hub.acked["worker"], 1000)
        self.assertIn(("worker", "1000"), self.hub.typing_stopped)

    async def test_multipart_append_edit_same_message_retry_and_restart(self):
        await self.hub.ingest(msg())
        first = await self.hub.say("lead", "1000", "hello ", 1)
        self.assertEqual(self.hub.acked["lead"], 1000)
        await self.restart()
        duplicate = await self.hub.say("lead", "1000", "hello ", 1)
        self.assertTrue(duplicate["duplicate"])
        second = await self.hub.say("lead", "1000", "world", 2, True)
        self.assertEqual(first["message_id"], second["message_id"])
        self.assertEqual(self.transport.sent[-1][2:4], ("hello world", first["message_id"]))
        self.assertEqual(len(self.transport.sent), 2)
        with self.assertRaises(BridgeError):
            await self.hub.say("lead", "1000", "more", 3)

    async def test_multipart_rejects_gaps_changed_retries_and_overflow(self):
        await self.hub.ingest(msg())
        with self.assertRaises(BridgeError):
            await self.hub.say("lead", "1000", "gap", 2)
        with self.assertRaises(BridgeError):
            await self.hub.say("lead", "1000", "😀" * 1001)
        await self.hub.say("lead", "1000", "part", 1)
        with self.assertRaises(BridgeError):
            await self.hub.say("lead", "1000", "different", 1)

    async def test_reaction_at_enqueue_does_not_block_or_wait_for_delivery(self):
        self.transport.block_react = asyncio.Event()
        await asyncio.wait_for(self.hub.ingest(msg()), .3)
        await eventually(lambda: bool(self.transport.reactions))
        self.assertEqual(self.hub.waiters["lead"], 0)
        self.assertEqual(self.transport.reactions, [("lead", "1000")])
        self.assertEqual(self.hub.acked["lead"], 0)

    async def test_typing_starts_at_enqueue_then_stops_at_idle_deadline(self):
        await self.hub.ingest(msg())
        await eventually(lambda: bool(self.transport.typings))
        await asyncio.sleep(.07)
        count = len(self.transport.typings)
        await asyncio.sleep(.04)
        self.assertEqual(len(self.transport.typings), count)
        self.assertGreater(count, 0)

    async def test_typing_stops_on_first_say(self):
        await self.hub.ingest(msg())
        await eventually(lambda: bool(self.transport.typings))
        await self.hub.say("lead", "1000", "first", 1)
        count = len(self.transport.typings)
        await asyncio.sleep(.04)
        self.assertEqual(len(self.transport.typings), count)

    async def test_active_wait_allows_typing_past_grace_then_stops(self):
        self.hub.ready.clear()
        waiter = asyncio.create_task(self.hub.wait("lead", 1))
        await asyncio.sleep(.005)
        await self.hub.ingest(msg())
        await asyncio.sleep(.08)
        count = len(self.transport.typings)
        self.assertGreater(count, 4)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        await asyncio.sleep(.03)
        stopped = len(self.transport.typings)
        await asyncio.sleep(.03)
        self.assertEqual(len(self.transport.typings), stopped)
        self.assertEqual(self.hub.waiters["lead"], 0)

    async def test_history_each_agent_after_own_cursor_recovers_downtime_and_dedup(self):
        await self.hub.ingest(msg(1000, "tất cả"))
        await self.hub.ingest(msg(1001, "tất cả"))
        await self.hub.ack("lead", "1000")
        await self.hub.ack("lead", "1001")
        await self.hub.ack("worker", "1000")
        calls = []
        async def history(agent, after):
            calls.append((agent, after))
            for m in [msg(1000, "tất cả"), msg(1001, "tất cả"), msg(1002, "tất cả"),
                      msg(1003, '"lead', author=999), msg(1004, '"lead', channel_id=99)]:
                if int(m.id) > after:
                    yield m
        await self.hub.recover(history)
        self.assertEqual(sorted(calls), [("deputy", 0), ("lead", 1001), ("worker", 1000)])
        self.assertEqual(list(self.hub.queues["lead"]), ["1002"])
        self.assertEqual(list(self.hub.queues["worker"]), ["1001", "1002"])
        self.assertEqual(list(self.hub.queues["deputy"]), ["1000", "1001", "1002"])
        self.assertEqual(len(self.hub.sources), 3)

    async def test_first_start_history_is_context_only_without_enqueue_effects(self):
        await self.hub.close()
        fresh_path = self.path / "first-run"
        self.hub = Hub(config(fresh_path), self.transport, typing_interval=.01, typing_grace=.04)
        self.assertIsNone(self.hub.previous_hub_start)
        old = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
        async def history(agent, after):
            if agent == "lead":
                yield msg(1000, '"lead please', timestamp=old)
                yield msg(1001, "room context", timestamp=old)

        await self.hub.recover(history)
        self.assertEqual(self.hub.queues["lead"], {})
        self.assertEqual(self.hub.queued_at, {})
        result = await self.hub.wait("lead", 0)
        self.assertEqual(result["messages"], [])
        self.assertEqual({entry["id"] for entry in result["context"]}, {"1000", "1001"})
        self.assertFalse(self.transport.reactions)
        self.assertFalse(self.transport.typings)
        rows = [json.loads(line) for line in self.hub.metrics_path.read_text(encoding="utf-8").splitlines()]
        self.assertNotIn("enqueue", [row["event"] for row in rows])
        await self.hub.close()
        journal_rows = [json.loads(line) for line in (fresh_path / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(row["type"] == "history_retire" and row["reason"] == "first_run_history"
                            for row in journal_rows))

    async def test_history_after_previous_start_is_recovered_and_old_history_is_not_requeued(self):
        await self.hub.recover(empty_history)
        await self.hub.close()
        previous_start = self.hub.current_hub_start
        downtime_message = msg(1000, '"lead during downtime', timestamp=datetime.fromtimestamp(
            previous_start + 3600, timezone.utc).isoformat())

        self.hub = Hub(config(self.path), self.transport, typing_interval=.01, typing_grace=.04)
        async def downtime_history(agent, after):
            if agent == "lead":
                yield downtime_message
        await self.hub.recover(downtime_history)
        self.assertEqual(list(self.hub.queues["lead"]), ["1000"])
        await self.hub.ack("lead", "1000")
        await self.hub.close()

        self.hub = Hub(config(self.path), self.transport, typing_interval=.01, typing_grace=.04)
        async def old_history(agent, after):
            if agent == "lead":
                yield downtime_message
                yield msg(1001, '"lead old history', timestamp=datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat())
        await self.hub.recover(old_history)
        self.assertEqual(self.hub.queues["lead"], {})
        self.assertEqual((await self.hub.wait("lead", 0))["messages"], [])

    async def test_failed_history_fetch_does_not_advance_recovery_boundary(self):
        await self.hub.recover(empty_history)
        boundary = self.hub.previous_hub_start
        message = msg(1000, '"lead during downtime', timestamp=datetime.fromtimestamp(
            boundary + 3600, timezone.utc).isoformat())

        async def broken_history(agent, after):
            if agent == "lead":
                yield message
                raise RuntimeError("temporary history failure")

        with self.assertRaises(RuntimeError):
            await self.hub.recover(broken_history)
        self.assertEqual(self.hub.previous_hub_start, boundary)

        async def retry_history(agent, after):
            if agent == "lead":
                yield message
        await self.hub.recover(retry_history)
        self.assertEqual(list(self.hub.queues["lead"]), ["1000"])

    async def test_restart_retires_legacy_stale_queue_but_preserves_delivered_work(self):
        await self.hub.close()
        journal_path = self.path / "journal.jsonl"
        marker = datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()
        old = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
        journal = Journal(journal_path)
        journal.append({"type": "hub_start", "at": marker})
        journal.append({"type": "message", "message": msg(1000, '"lead stale', timestamp=old).public()})
        journal.append({"type": "message", "message": msg(1002, '"lead already delivered', timestamp=old).public()})
        journal.append({"type": "delivery", "agent": "lead", "id": "1002", "state": {
            "first_at": 1, "delivered_at": 1, "received": False, "had_ack1": False,
            "lease_until": None, "work_started": None, "extensions": 0,
            "redelivered": 0, "due": False}})
        journal.close()

        self.hub = Hub(config(self.path), self.transport, typing_interval=.01, typing_grace=.04)
        self.assertEqual(set(self.hub.queues["lead"]), {"1002"})
        await self.hub.recover(empty_history)
        await self.hub.close()
        records = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
        retirements = [row for row in records if row["type"] == "history_retire"]
        self.assertEqual([(row["agent"], row["id"]) for row in retirements], [("lead", "1000")])
        metric_rows = [json.loads(line) for line in self.hub.metrics_path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(row["event"] == "history_retire" and row["id"] == "1000" for row in metric_rows))

    async def test_history_live_race_sorted_and_owner_reset_kept(self):
        for i in range(8):
            await self.hub.ingest(msg(1000 + i, author=102))
        async def history(agent, after):
            if agent == "lead":
                await self.hub.ingest(msg(1010, author=102))
                yield msg(1009, '"worker')
                yield msg(1010, author=102)
        await self.hub.recover(history)
        self.assertIn("1010", self.hub.queues["lead"])
        self.assertEqual(self.hub.filter.counts["lead"], 1)

    async def test_failed_history_holds_wait_and_buffer_for_retry(self):
        async def broken(agent, after):
            if agent == "lead":
                await self.hub.ingest(msg())
                raise RuntimeError("history unavailable")
            if False:
                yield None
        with self.assertRaises(RuntimeError):
            await self.hub.recover(broken)
        self.assertFalse(self.hub.ready.is_set())
        self.assertIn("1000", self.hub.buffer)
        self.assertEqual(await self.hub.wait("lead", .01), {"messages": [], "context": [], "heartbeat": True})
        await self.hub.recover(empty_history)
        self.assertEqual((await self.hub.wait("lead", 0))["messages"][0]["id"], "1000")

    async def test_late_duplicate_owner_cannot_reset_peer_circuit(self):
        await self.hub.ingest(msg(999, "ordinary"))
        for i in range(8):
            await self.hub.ingest(msg(1000 + i, author=102))
        await self.hub.ingest(msg(999, "ordinary"))
        self.assertEqual(await self.hub.ingest(msg(1008, author=102)), [])

    async def test_journal_failure_cannot_advance_ack(self):
        await self.hub.ingest(msg())
        self.hub.journal.failed = True
        with self.assertRaises(BridgeError):
            await self.hub.ack("lead", "1000")
        self.assertEqual(self.hub.acked["lead"], 0)

    async def test_restart_retains_peer_breaker_and_ordinary_owner_reset(self):
        for i in range(8):
            await self.hub.ingest(msg(1000 + i, author=102))
        await self.restart()
        self.assertEqual(await self.hub.ingest(msg(1008, author=102)), [])
        await self.hub.ingest(msg(1009, "ordinary owner message"))
        self.assertEqual(await self.hub.ingest(msg(1010, author=102)), ["lead"])

    async def test_batch_limit_never_silently_discards_remaining_work(self):
        for i in range(101):
            await self.hub.ingest(msg(1000 + i))
        result = await self.hub.wait("lead", 0)
        self.assertEqual(len(result["messages"]), 100)
        self.assertTrue(result["more"])
        await self.hub.ack("lead", result["messages"][-1]["id"])
        result = await self.hub.wait("lead", 0)
        self.assertEqual([m["id"] for m in result["messages"]], ["1100"])
        self.assertFalse(result["more"])

    async def test_one_delivery_append_failure_does_not_abort_the_batch(self):
        for i in range(3):
            await self.hub.ingest(msg(1000 + i))
        original_append = self.hub.journal.append
        failed = False
        original_fsync = __import__("os").fsync

        def fail_one_delivery(record):
            nonlocal failed
            if record.get("type") == "delivery" and record.get("id") == "1001" and not failed:
                failed = True
                fsync_failed = False

                def fail_once(fd):
                    nonlocal fsync_failed
                    if not fsync_failed:
                        fsync_failed = True
                        raise OSError("simulated fsync failure")
                    return original_fsync(fd)

                with patch("bridgehub.core.os.fsync", side_effect=fail_once):
                    return original_append(record)
            return original_append(record)

        self.hub.journal.append = fail_one_delivery
        with self.assertLogs("bridgehub.core", level=logging.ERROR) as captured:
            result = await self.hub.wait("lead", 0)
        self.hub.journal.append = original_append
        self.assertEqual([m["id"] for m in result["messages"]], ["1000", "1002"])
        self.assertTrue(result["more"])
        self.assertNotIn(("lead", "1001"), self.hub.delivery)
        self.assertIn("1001", self.hub.queues["lead"])
        self.assertFalse(self.hub.journal.failed)
        self.assertIn("OSError", "\n".join(captured.output))
        retry = await self.hub.wait("lead", 0)
        self.assertEqual([m["id"] for m in retry["messages"]], ["1001"])

    async def test_late_owner_event_rebuilds_in_snowflake_order(self):
        for i in range(8):
            await self.hub.ingest(msg(1000 + i, author=102))
        self.assertEqual(await self.hub.ingest(msg(1010, author=102)), [])
        await self.hub.ingest(msg(1009, "late owner room message"))
        self.assertIn("1010", self.hub.queues["lead"])
        self.assertEqual(self.hub.filter.counts["lead"], 1)


def test_torn_journal_fails_closed(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_bytes(b'{"v":1,"type":"ack"')
    with __import__("pytest").raises(BridgeError, match="Incomplete"):
        Journal(path)


def test_second_writer_is_rejected(tmp_path):
    first = Journal(tmp_path / "lock.jsonl")
    try:
        with __import__("pytest").raises(OSError):
            Journal(tmp_path / "lock.jsonl")
    finally:
        first.close()
