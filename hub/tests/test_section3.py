"""Offline lifecycle tests: fake monotonic time, real durable JSONL state."""
import asyncio
import json
import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest

from bridgehub.core import BridgeError, Filter, Hub
from bridgehub.discord_runtime import DiscordRuntime
from bridgehub.protocol import dispatch
from .helpers import FakeTransport, config, empty_history, eventually, msg
from .test_config import environment
from .test_protocol import load_cli


class Clock:
    def __init__(self):
        self.now = 0.

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.clock = Clock()
        self.transport = FakeTransport()
        self.cfg = config(self.path)
        self.hub = Hub(self.cfg, self.transport, clock=self.clock, sweeper_interval=10000)
        await self.hub.recover(empty_history)

    async def asyncTearDown(self):
        await self.hub.close()
        self.temp.cleanup()

    async def restart(self):
        await self.hub.close()
        self.clock.advance(100000)
        self.hub = Hub(self.cfg, self.transport, clock=self.clock, sweeper_interval=10000)
        await self.hub.recover(empty_history)

    async def deliver(self, text='"lead', mid=1000):
        await self.hub.ingest(msg(mid, text))
        return await self.hub.wait("lead", 0)

    async def test_long_poll_caps_at_50_and_keeps_context(self):
        await self.hub.ingest(msg(999, "context"))
        captured = []
        async def expire(coro, timeout):
            captured.append(timeout)
            coro.close()
            raise asyncio.TimeoutError
        with patch("bridgehub.core.asyncio.wait_for", side_effect=expire):
            heartbeat = await self.hub.wait("lead", 540)
            await self.hub.wait("lead", 12)
        self.assertEqual(captured, [50, 12])
        self.assertTrue(heartbeat["heartbeat"])
        self.assertEqual(heartbeat["messages"], [])
        self.assertEqual(heartbeat["context"][0]["id"], "999")
        self.assertTrue(heartbeat["context"][0]["do_not_reply"])

    async def test_liveness_records_wait_even_at_zero_and_expires_after_100(self):
        await self.hub.ingest(msg())
        self.assertFalse(self.hub.alive("lead"))
        await self.hub.wait("lead", 0)
        for _ in range(3):
            self.clock.advance(50)
            self.assertTrue(self.hub.alive("lead"))
            await self.hub.wait("lead", 0)
        self.clock.advance(100)
        self.assertTrue(self.hub.alive("lead"))
        self.clock.advance(.1)
        self.assertFalse(self.hub.alive("lead"))

    async def test_working_extends_five_minutes_without_cursor_movement(self):
        await self.deliver()
        self.clock.advance(10)
        result = await dispatch(self.hub, {"command": "working", "agent": "lead", "eta": 2})
        self.assertEqual(result["leases"], [{"id": "1000", "remaining_seconds": 300}])
        self.clock.advance(10)
        result = await self.hub.working("lead", "1000")
        self.assertEqual(result["leases"][0]["remaining_seconds"], 590)
        self.assertEqual(self.hub.acked["lead"], 0)
        self.assertIn("1000", self.hub.queues["lead"])
        self.assertTrue((await self.hub.wait("lead", 0))["heartbeat"])

    async def test_working_total_lease_cap_survives_restart(self):
        await self.deliver()
        for _ in range(5):
            await self.hub.working("lead")
        await self.restart()
        await self.hub.working("lead")
        with self.assertRaisesRegex(BridgeError, "30 minutes"):
            await self.hub.working("lead")

    async def test_invalid_working_requests_never_advance(self):
        await self.hub.ingest(msg())
        with self.assertRaises(BridgeError):
            await self.hub.working("lead")
        await self.hub.wait("lead", 0)
        for eta in (True, 0, -1, "3", math.nan, math.inf):
            with self.assertRaises(BridgeError):
                await self.hub.working("lead", "1000", eta)
        self.assertEqual(self.hub.acked["lead"], 0)

    async def test_receive_expiry_redelivers_only_after_sweep_and_includes_context(self):
        await self.deliver()
        self.clock.advance(181)
        self.assertTrue((await self.hub.wait("lead", 0))["heartbeat"])
        await self.hub.sweep()
        message = (await self.hub.wait("lead", 0))["messages"][0]
        self.assertEqual(message["redelivered"], 1)
        self.assertFalse(message["had_ack1"])
        self.assertEqual(message["parts"], [])
        self.assertEqual(self.hub.acked["lead"], 0)

    async def test_single_periodic_sweeper_wakes_a_blocked_wait(self):
        await self.deliver()
        self.hub.sweeper_task.cancel()
        await asyncio.gather(self.hub.sweeper_task, return_exceptions=True)
        self.hub.sweeper_task = None
        self.hub.sweeper_interval = .01
        self.hub.start_maintenance()
        worker = self.hub.sweeper_task
        self.hub.start_maintenance()
        self.assertIs(self.hub.sweeper_task, worker)
        waiting = asyncio.create_task(self.hub.wait("lead", 1))
        await eventually(lambda: self.hub.waiters["lead"] == 1)
        self.clock.advance(181)
        result = await waiting
        self.assertEqual(result["messages"][0]["redelivered"], 1)

    async def test_redelivery_carries_ack1_and_durable_attempt_count(self):
        await self.deliver()
        await self.hub.working("lead")
        self.clock.advance(301)
        await self.hub.sweep()
        redelivery = (await self.hub.wait("lead", 0))["messages"][0]
        self.assertTrue(redelivery["had_ack1"])
        await self.restart()
        # After the restart rebase, first_at is the restart time and the
        # message already used R1, so R2 falls due at +300 (1/5/15 schedule).
        self.clock.advance(300)
        await self.hub.sweep()
        self.assertEqual((await self.hub.wait("lead", 0))["messages"][0]["redelivered"], 2)

    async def test_three_redeliveries_then_suspect_does_not_block_next_message(self):
        await self.deliver()
        # Redelivery schedule is now fixed at minutes 1/5/15 from first_at.
        for advance_by, count in ((60, 1), (240, 2), (600, 3)):
            self.clock.advance(advance_by)
            await self.hub.sweep()
            self.assertEqual((await self.hub.wait("lead", 0))["messages"][0]["redelivered"], count)
        self.clock.advance(60)
        await self.hub.sweep()
        self.assertIn("1000", self.hub.suspects["lead"])
        await self.hub.ingest(msg(1001))
        self.assertEqual([m["id"] for m in (await self.hub.wait("lead", 0))["messages"]], ["1001"])
        await self.hub.ack("lead", "1001")
        self.assertEqual(self.hub.acked["lead"], 0)
        await self.restart()
        self.assertIn("1000", self.hub.suspects["lead"])
        self.assertNotIn("1001", self.hub.queues["lead"])
        await self.hub.ack("lead", "1000")
        self.assertEqual(self.hub.acked["lead"], 1001)
        await self.restart()
        self.assertEqual(self.hub.suspects["lead"], set())

    async def test_ack_larger_id_preserves_gap_across_restart_and_late_rebuild(self):
        await self.hub.ingest(msg(1000))
        await self.hub.ingest(msg(1002))
        await self.hub.ack("lead", "1002")
        self.assertEqual(self.hub.acked["lead"], 0)
        await self.restart()
        await self.hub.ingest(msg(1001))
        self.assertEqual(set(self.hub.queues["lead"]), {"1000", "1001"})
        await self.hub.ack("lead", "1000")
        self.assertEqual(self.hub.acked["lead"], 1000)
        await self.hub.ack("lead", "1001")
        self.assertEqual(self.hub.acked["lead"], 1002)

    async def test_say_larger_id_preserves_gap_across_restart(self):
        await self.hub.ingest(msg(1000))
        await self.hub.ingest(msg(1001))
        await self.hub.say("lead", "1001", "answer")
        await self.restart()
        self.assertEqual(self.hub.acked["lead"], 0)
        self.assertEqual(set(self.hub.queues["lead"]), {"1000"})
        await self.hub.ack("lead", "1000")
        self.assertEqual(self.hub.acked["lead"], 1001)

    async def test_never_waited_agent_warns_after_five_minutes_only(self):
        await self.hub.ingest(msg())
        self.clock.advance(300)
        await self.hub.sweep()
        self.assertEqual(self.transport.warnings, [])
        self.clock.advance(.1)
        await self.hub.sweep()
        warning = self.transport.warnings[0]
        self.assertIn("lead", warning[0])
        self.assertIn("chưa nhận", warning[0])
        self.assertNotIn("@", warning[0])
        self.assertNotIn('"lead', warning[0])
        self.assertFalse(warning[2])
        await self.hub.sweep()
        self.assertEqual(len(self.transport.warnings), 1)

    async def test_live_wait_and_unexpired_lease_does_not_warn(self):
        await self.deliver()
        await self.hub.working("lead")
        self.clock.advance(250)
        await self.hub.working("lead")
        self.clock.advance(51)
        await self.hub.wait("lead", 0)
        await self.hub.sweep()
        self.assertEqual(self.transport.warnings, [])

    async def test_expired_lease_warns_received_but_silent_even_if_wait_alive(self):
        await self.deliver()
        await self.hub.working("lead")
        self.clock.advance(301)
        await self.hub.wait("lead", 0)
        await self.hub.sweep()
        self.assertIn("nhận rồi im", self.transport.warnings[0][0])

    async def test_escalation_once_at_15_then_recovery_edits_original(self):
        # Escalation threshold changed from 1800s of message age (old tier 2)
        # to ESCALATE_AFTER=900s measured from deaf_since (spec section 3.4).
        await self.hub.ingest(msg())
        self.clock.advance(301)
        await self.hub.sweep()
        first_id = self.transport.warnings[0][3]
        self.clock.advance(898)
        await self.hub.sweep()
        self.assertEqual(len(self.transport.warnings), 1)
        self.clock.advance(1)
        await self.hub.sweep()
        self.assertIn("<@1>", self.transport.warnings[1][0])
        self.assertTrue(self.transport.warnings[1][2])
        await self.hub.sweep()
        self.assertEqual(len(self.transport.warnings), 2)
        await self.hub.ack("lead", "1000")
        self.assertEqual(self.transport.warnings[2][1], first_id)
        self.assertIn("✅ lead đã hồi", self.transport.warnings[2][0])
        self.assertEqual(len(self.transport.warnings), 4)
        await self.hub.ack("lead", "1000")
        self.assertEqual(len(self.transport.warnings), 4)

    async def test_simultaneous_agents_merge_and_partial_recoveries_edit_same_message(self):
        await self.hub.ingest(msg(1000, '"lead "worker'))
        self.clock.advance(301)
        await self.hub.sweep()
        self.assertEqual(len(self.transport.warnings), 1)
        self.assertIn("lead", self.transport.warnings[0][0])
        self.assertIn("worker", self.transport.warnings[0][0])
        first_id = self.transport.warnings[0][3]
        await self.hub.ack("lead", "1000")
        self.assertEqual(self.transport.warnings[-1][1], first_id)
        self.assertIn("worker: chưa nhận", self.transport.warnings[-1][0])
        await self.hub.ack("worker", "1000")
        self.assertEqual(self.transport.warnings[-1][1], first_id)
        self.assertEqual(len(self.transport.warnings), 3)

    async def test_new_failed_agent_joins_existing_warning(self):
        await self.hub.ingest(msg(1000))
        self.clock.advance(301)
        await self.hub.sweep()
        await self.hub.ingest(msg(1001, '"worker'))
        self.clock.advance(301)
        await self.hub.sweep()
        self.assertEqual(len(self.transport.warnings), 2)
        self.assertEqual(self.transport.warnings[1][1], self.transport.warnings[0][3])
        self.assertIn("worker", self.transport.warnings[1][0])

    async def test_restart_preserves_warning_tiers_and_ids_and_resets_wait_clock(self):
        await self.hub.ingest(msg())
        self.clock.advance(1800)
        await self.hub.sweep()
        self.assertEqual(len(self.transport.warnings), 2)
        first_id = self.transport.warnings[0][3]
        await self.restart()
        self.clock.advance(599)
        await self.hub.sweep()
        self.assertEqual(len(self.transport.warnings), 2)
        self.clock.advance(2000)
        await self.hub.sweep()
        self.assertEqual(len(self.transport.warnings), 2)
        await self.hub.ack("lead", "1000")
        self.assertEqual(self.transport.warnings[2][1], first_id)

    async def test_restart_unwarned_queue_has_five_minute_grace_plus_wait_age(self):
        await self.hub.ingest(msg())
        await self.restart()
        self.clock.advance(600)
        await self.hub.sweep()
        self.assertEqual(self.transport.warnings, [])
        self.clock.advance(.1)
        await self.hub.sweep()
        self.assertEqual(len(self.transport.warnings), 1)

    async def test_failed_warning_and_recovery_are_retried_without_reacking(self):
        await self.hub.ingest(msg())
        self.transport.fail_warning = True
        self.clock.advance(301)
        with self.assertRaises(RuntimeError):
            await self.hub.sweep()
        self.transport.fail_warning = False
        await self.hub.sweep()
        self.assertEqual(len(self.transport.warnings), 1)
        self.transport.fail_warning = True
        await self.hub.ack("lead", "1000")
        self.assertEqual(self.hub.acked["lead"], 1000)
        self.transport.fail_warning = False
        await self.hub.sweep()
        self.assertEqual(len(self.transport.warnings), 2)
        self.assertIsNotNone(self.transport.warnings[-1][1])

    async def test_duplicate_parts_no_send_but_second_final_rejected_after_restart(self):
        await self.hub.ingest(msg())
        await self.hub.say("lead", "1000", "one", 1)
        self.assertTrue((await self.hub.say("lead", "1000", "one", 1))["duplicate"])
        self.assertEqual(len(self.transport.sent), 1)
        await self.hub.say("lead", "1000", "two", 2, True)
        await self.restart()
        with self.assertRaisesRegex(BridgeError, "second --final"):
            await self.hub.say("lead", "1000", "two", 2, True)
        self.assertEqual(len(self.transport.sent), 2)

    async def test_p95_deadline_uses_measured_ack1_and_restores(self):
        self.assertEqual(self.hub.receive_deadline("worker"), 300)
        self.assertEqual(self.hub.receive_deadline("lead"), 180)
        await self.deliver()
        self.clock.advance(130)
        await self.hub.working("lead")
        self.assertEqual(self.hub.receive_deadline("lead"), 260)
        await self.restart()
        self.assertEqual(self.hub.receive_deadline("lead"), 260)

    async def test_metrics_capture_lifecycle_and_latency_fields(self):
        await self.deliver()
        self.clock.advance(15)
        await self.hub.working("lead")
        self.clock.advance(10)
        await self.hub.ack("lead", "1000")
        await self.hub.close()
        rows = [json.loads(line) for line in self.hub.metrics_path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue({"hub_start", "enqueue", "wait", "delivery", "ack1", "ack2", "hub_stop"} <= {r["event"] for r in rows})
        self.assertEqual(next(r for r in rows if r["event"] == "ack1")["latency"], 15)
        self.assertEqual(next(r for r in rows if r["event"] == "ack2")["latency"], 25)
        self.assertNotEqual(self.hub.metrics_path, Path(self.cfg.journal_path))

    async def test_health_ping_only_when_enabled_connected_and_listening(self):
        self.hub.config = replace(self.cfg, healthcheck_ping_url="https://offline.invalid/ping")
        with patch("bridgehub.reliability.urllib.request.urlopen") as call:
            await self.hub._health_ping()
            call.assert_not_called()
            self.hub.socket_listening = True
            self.transport.connected = False
            await self.hub._health_ping()
            call.assert_not_called()
            self.transport.connected = True
            await self.hub._health_ping()
            self.assertEqual(call.call_count, 1)
            self.hub.config = self.cfg
            await self.hub._health_ping()
            self.assertEqual(call.call_count, 1)

    async def test_health_ping_cadence_is_sixty_seconds(self):
        with patch.object(self.hub, "_health_ping", new_callable=AsyncMock) as ping:
            self.clock.advance(59)
            await self.hub.sweep()
            ping.assert_not_called()
            self.clock.advance(1)
            await self.hub.sweep()
            self.clock.advance(10)
            await self.hub.sweep()
            self.assertEqual(ping.await_count, 1)
            self.clock.advance(50)
            await self.hub.sweep()
            self.assertEqual(ping.await_count, 2)

    def metric_rows(self, event=None):
        rows = [json.loads(line) for line in
                self.hub.metrics_path.read_text(encoding="utf-8").splitlines()]
        return [r for r in rows if event is None or r["event"] == event]

    # --- Issue: hub-side deaf detection + escalation (spec sections 3.2-3.4)

    async def test_deaf_flagged_at_five_minutes_with_pending(self):
        await self.deliver()
        self.clock.advance(301)
        self.hub._deaf_check(self.clock())
        entry = self.hub.deaf_state["lead"]
        self.assertEqual(entry["state"], "deaf")
        self.assertEqual(entry["deaf_since"], self.hub.last_wait_at["lead"] + 300)
        rows = [r for r in self.metric_rows("deaf") if r["agent"] == "lead"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pending"], 1)
        self.assertIn("deaf_since_wall", rows[0])

    async def test_idle_logged_not_warned_without_pending(self):
        self.clock.advance(301)
        self.hub._deaf_check(self.clock())
        self.assertEqual(self.hub.deaf_state["lead"]["state"], "idle")
        rows = [r for r in self.metric_rows("idle") if r["agent"] == "lead"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.transport.warnings, [])

    async def test_stale_band_does_not_flag(self):
        await self.deliver()
        self.clock.advance(150)
        self.hub._deaf_check(self.clock())
        self.assertEqual(self.hub.deaf_state["lead"]["state"], "stale")
        self.assertEqual(self.metric_rows("deaf"), [])
        self.assertEqual(self.transport.warnings, [])

    async def test_deaf_recovers_on_next_wait(self):
        await self.deliver()
        self.clock.advance(301)
        self.hub._deaf_check(self.clock())
        self.assertEqual(self.hub.deaf_state["lead"]["state"], "deaf")
        await self.hub.wait("lead", 0)
        self.hub._deaf_check(self.clock())
        self.assertEqual(self.hub.deaf_state["lead"]["state"], "alive")
        rows = [r for r in self.metric_rows("recovered_deaf") if r["agent"] == "lead"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["silent_seconds"], 1)

    async def test_escalation_once_at_15_minutes_per_agent(self):
        await self.deliver()
        self.clock.advance(301)
        self.hub._deaf_check(self.clock())
        deaf_since = self.hub.deaf_state["lead"]["deaf_since"]
        self.clock.advance(deaf_since + 900 - self.clock())
        await self.hub._warn(self.clock())
        mentions = [w for w in self.transport.warnings if w[2]]
        self.assertEqual(len(mentions), 1)
        self.assertIn("lead", mentions[0][0])
        self.assertIn("ĐIẾC", mentions[0][0])
        self.assertIn("1 tin đang kẹt", mentions[0][0])
        rows = [r for r in self.metric_rows("escalation") if r["agent"] == "lead"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pending"], 1)
        self.assertEqual(rows[0]["pending_ids"], ["1000"])
        self.clock.advance(60)
        await self.hub._warn(self.clock())
        self.assertEqual(len([w for w in self.transport.warnings if w[2]]), 1)

    async def test_second_agent_escalates_independently(self):
        # Regression test for the group-tier2 bug: with the old group-level
        # condition the second agent would never be escalated.
        await self.hub.ingest(msg(1000, '"lead "worker'))
        await self.hub.wait("lead", 0)
        await self.hub.wait("worker", 0)
        self.clock.advance(301)
        self.hub._deaf_check(self.clock())
        self.assertEqual(self.hub.deaf_state["lead"]["state"], "deaf")
        self.assertEqual(self.hub.deaf_state["worker"]["state"], "deaf")
        self.clock.advance(899)
        await self.hub._warn(self.clock())
        mentions = [w[0] for w in self.transport.warnings if w[2]]
        self.assertEqual(len(mentions), 2)
        self.assertTrue(any("lead" in text for text in mentions))
        self.assertTrue(any("worker" in text for text in mentions))

    async def test_redelivery_schedule_1_5_15(self):
        await self.deliver()
        state = lambda: self.hub.delivery[("lead", "1000")]
        self.clock.advance(59)
        await self.hub.sweep()
        self.assertFalse(state()["due"])
        self.clock.advance(1)
        await self.hub.sweep()
        self.assertTrue(state()["due"])
        self.assertEqual((await self.hub.wait("lead", 0))["messages"][0]["redelivered"], 1)
        self.clock.advance(239)
        await self.hub.sweep()
        self.assertFalse(state()["due"])
        self.clock.advance(1)
        await self.hub.sweep()
        self.assertTrue(state()["due"])
        self.assertEqual((await self.hub.wait("lead", 0))["messages"][0]["redelivered"], 2)
        self.clock.advance(599)
        await self.hub.sweep()
        self.assertFalse(state()["due"])
        self.clock.advance(1)
        await self.hub.sweep()
        self.assertTrue(state()["due"])
        self.assertEqual((await self.hub.wait("lead", 0))["messages"][0]["redelivered"], 3)
        await self.hub.sweep()
        self.assertIn("1000", self.hub.suspects["lead"])

    async def test_working_lease_ignores_redelivery_schedule(self):
        await self.deliver()
        await self.hub.working("lead")
        self.clock.advance(300)
        await self.hub.sweep()
        self.assertFalse(self.hub.delivery[("lead", "1000")]["due"])
        self.assertIn("1000", self.hub.queues["lead"])
        self.clock.advance(1)
        await self.hub.sweep()
        self.assertTrue(self.hub.delivery[("lead", "1000")]["due"])

    async def test_restart_grace_before_deaf_flag(self):
        await self.deliver()
        await self.restart()
        self.clock.advance(299)
        self.hub._deaf_check(self.clock())
        self.assertNotEqual(self.hub.deaf_state["lead"]["state"], "deaf")
        self.clock.advance(2)
        self.hub._deaf_check(self.clock())
        entry = self.hub.deaf_state["lead"]
        self.assertEqual(entry["state"], "deaf")
        self.assertEqual(entry["deaf_since"], self.hub.started_at + 300)

    async def test_recovery_after_escalation_edits_and_reports_duration(self):
        await self.deliver()
        self.clock.advance(301)
        self.hub._deaf_check(self.clock())
        self.clock.advance(899)
        await self.hub._warn(self.clock())
        self.assertEqual(len([w for w in self.transport.warnings if w[2]]), 1)
        await self.hub.ack("lead", "1000")
        joined = "\n".join(w[0] for w in self.transport.warnings)
        self.assertIn("✅ lead đã hồi sau 15.0 phút", joined)
        self.assertIn("Bridge recovered: lead (điếc 15.0 phút)", joined)

    async def test_dedicated_hub_messages_are_not_authorized_or_counted(self):
        cfg = replace(self.cfg, hub_bot_id=104, hub_bot_token="offline-hub")
        filt = Filter(cfg)
        filt.route(msg(author=102))
        before = dict(filt.counts)
        self.assertEqual(filt.route(msg(1001, '"lead tất cả', author=104)), [])
        self.assertEqual(filt.counts, before)

    async def test_fallback_warning_id_excluded_before_filter_even_after_restart(self):
        await self.hub.ingest(msg())
        self.clock.advance(301)
        await self.hub.sweep()
        mid = self.transport.warnings[0][3]
        await self.restart()
        before = dict(self.hub.filter.counts)
        self.assertEqual(await self.hub.ingest(msg(mid, '"lead tất cả', author=101)), [])
        self.assertNotIn(mid, self.hub.sources)
        self.assertEqual(self.hub.filter.counts, before)

    async def test_warning_adapter_disables_mentions_except_owner_escalation(self):
        runtime = DiscordRuntime(self.cfg)
        channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=9000)))
        partial = SimpleNamespace(edit=AsyncMock(return_value=SimpleNamespace(id=9000)))
        channel.get_partial_message = lambda mid: partial
        runtime.channels["lead"] = channel
        runtime.online["lead"].set()
        await runtime.send_warning("lead: chưa nhận")
        self.assertEqual(channel.send.call_args.kwargs["allowed_mentions"].to_dict()["parse"], [])
        await runtime.send_warning("<@1> escalation", mention=True)
        allowed = channel.send.call_args.kwargs["allowed_mentions"].to_dict()
        self.assertEqual(allowed["users"], [1])
        self.assertNotIn("everyone", allowed["parse"])
        await runtime.send_warning("✅ lead đã hồi", "9000")
        partial.edit.assert_awaited_once()
        self.assertTrue(runtime._hub_message(SimpleNamespace(id=9000, content="ignored", author=SimpleNamespace(id=101))))


@pytest.mark.parametrize("key,value", [("LEAD_RECEIVE_DEADLINE", "nan"), ("WORKER_RECEIVE_DEADLINE", "inf"),
    ("DEPUTY_RECEIVE_DEADLINE", "179"), ("HUB_BOT_ID", "101"), ("BRIDGE_EXTERNAL_ALERTS_ENABLED", "yes")])
def test_new_config_rejects_invalid_values(key, value):
    from bridgehub.config import Config
    import os
    env = {**environment(), key: value}
    with patch.dict(os.environ, env, clear=True), pytest.raises(ValueError):
        Config.from_env()


def test_section3_environment_configuration():
    from bridgehub.config import Config
    import os
    env = {**environment(), "LEAD_RECEIVE_DEADLINE": "240", "BRIDGE_METRICS": "metrics.jsonl",
           "HEALTHCHECK_PING_URL": "https://offline.invalid/ping"}
    with patch.dict(os.environ, env, clear=True):
        cfg = Config.from_env()
    assert cfg.receive_deadlines == {"lead": 240, "worker": 300, "deputy": 180}
    assert cfg.metrics_path == "metrics.jsonl"
    assert cfg.healthcheck_ping_url == env["HEALTHCHECK_PING_URL"]
    assert not cfg.external_alerts_enabled
