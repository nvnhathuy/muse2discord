"""Integration tests: shared registry hot reload, routing gate,
bot lifecycle, live status facade, and TOTP step-up in the admin app.

Run from the tich-hop-hub folder:
    python3 -m unittest discover -s tests_integration -v
Everything runs against fakes - no Discord, no real hub, no NAS.
"""

import asyncio
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.abspath(os.path.join(ROOT, "hub")))
sys.path.insert(0, os.path.abspath(os.path.join(ROOT, "admin")))

from hub_integration.bot_lifecycle import BotLifecycle
from hub_integration.registry_sync import RegistryView, RegistryWatcher, bootstrap_state
from hub_integration.routing_gate import RoutingGate
from hub_integration.status_snapshot import DeliveryFacade, HubSnapshot

from hub_admin.registry import AgentRegistry
from hub_admin.secrets_store import SecretStore


def make_state(tmp, env=None):
    env = env or {
        "AGENT_KEYS": "alpha,beta",
        "ALPHA_DISPLAY_NAME": "Alpha", "ALPHA_BOT_ID": "111",
        "ALPHA_CALL_TOKENS": '["alpha"]', "ALPHA_BOT_TOKEN": "fake-token-alpha",
        "BETA_DISPLAY_NAME": "Beta", "BETA_BOT_ID": "222",
        "BETA_CALL_TOKENS": '["beta"]', "BETA_BOT_TOKEN": "fake-token-beta",
    }
    return bootstrap_state(tmp, env)


def admin_side(tmp):
    """A second registry handle, as the admin process would hold."""
    secrets = SecretStore(os.path.join(tmp, "bot_secrets.json"))
    return AgentRegistry(os.path.join(tmp, "agents.json"), secret_meta=secrets.meta), secrets


class RegistryHotReloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        registry, secrets = make_state(self.tmp)
        self.view = RegistryView(registry, secrets)
        self.watcher = RegistryWatcher(self.view)

    def test_bootstrap_from_env(self):
        agents = self.watcher.agents
        self.assertEqual(set(agents), {"alpha", "beta"})
        self.assertEqual(agents["alpha"].token, "fake-token-alpha")
        self.assertTrue(agents["alpha"].enabled)

    def test_no_events_when_unchanged(self):
        self.assertEqual(self.watcher.poll(), [])

    def test_pause_event_and_routing_effect(self):
        registry, _ = admin_side(self.tmp)
        registry.set_enabled("alpha", False, by="admin", reason="loi")
        events = self.watcher.poll()
        self.assertEqual([(e.kind, e.key) for e in events], [("enabled_changed", "alpha")])
        gate = RoutingGate(self.watcher.agents)
        self.assertEqual(gate.route(content='"alpha xin chao'), [])
        self.assertEqual(gate.route(content='"beta xin chao'), ["beta"])
        registry.set_enabled("alpha", True, by="admin")
        self.watcher.poll()
        gate.refresh(self.watcher.agents)
        self.assertEqual(gate.route(content='"alpha xin chao'), ["alpha"])

    def test_token_change_event(self):
        _, secrets = admin_side(self.tmp)
        secrets.set("alpha", "fake-token-alpha-2")
        events = self.watcher.poll()
        self.assertIn(("token_changed", "alpha"), [(e.kind, e.key) for e in events])
        self.assertEqual(self.watcher.agents["alpha"].token, "fake-token-alpha-2")

    def test_add_and_remove_events(self):
        registry, _ = admin_side(self.tmp)
        registry.create({"key": "gamma", "display_name": "Gamma", "call_tokens": ["gamma"]})
        registry.hard_delete("beta", lambda key: {"pending": 0, "leases": 0})
        kinds = {(e.kind, e.key) for e in self.watcher.poll()}
        self.assertIn(("added", "gamma"), kinds)
        self.assertIn(("removed", "beta"), kinds)

    def test_call_token_change_repatterns(self):
        registry, _ = admin_side(self.tmp)
        registry.update("alpha", {"call_tokens": ["alfa"]})
        events = self.watcher.poll()
        self.assertIn(("profile_changed", "alpha"), [(e.kind, e.key) for e in events])
        gate = RoutingGate(self.watcher.agents)
        self.assertEqual(gate.route(content='"alfa oi'), ["alpha"])
        self.assertEqual(gate.route(content='"alpha oi'), [])


class FakeConnector:
    def __init__(self):
        self.calls = []

    async def connect(self, key, token):
        self.calls.append(("connect", key, token))

    async def disconnect(self, key):
        self.calls.append(("disconnect", key))


class BotLifecycleTests(unittest.TestCase):
    def test_event_mapping(self):
        connector = FakeConnector()
        lifecycle = BotLifecycle(connector)
        registry, secrets = make_state(tempfile.mkdtemp())
        view = RegistryView(registry, secrets)
        watcher = RegistryWatcher(view)
        asyncio.run(lifecycle.start_all(watcher.agents))
        self.assertEqual([c[:2] for c in connector.calls],
                         [("connect", "alpha"), ("connect", "beta")])
        connector.calls.clear()
        # token change -> reconnect; pause -> no gateway action
        secrets2 = SecretStore(secrets.path)
        secrets2.set("alpha", "fake-token-alpha-9")
        registry.set_enabled("beta", False, by="admin", reason="x")
        events = watcher.poll()
        asyncio.run(lifecycle.apply(events, watcher.agents))
        self.assertEqual(connector.calls,
                         [("disconnect", "alpha"), ("connect", "alpha", "fake-token-alpha-9")])


class FakeMessage:
    def __init__(self, mid, author="Owner", ts="2026-11-22T03:00:00+00:00"):
        self.id = mid
        self.author_name = author
        self.timestamp = ts


class FakeJournal:
    failed = False


class FakeHub:
    """Duck-typed stand-in for bridgehub.core.Hub's state surface."""

    def __init__(self):
        import asyncio as _a
        self.queues = {"alpha": {"100": FakeMessage("100"), "101": FakeMessage("101")}, "beta": {}}
        self.acked = {"alpha": 99, "beta": 0}
        self.clock = time.monotonic
        now = self.clock()
        self.delivery = {
            ("alpha", "100"): {"lease_until": now + 120, "redelivered": 1, "due": False,
                               "received": True, "first_at": now, "delivered_at": now},
            ("alpha", "101"): {"lease_until": None, "redelivered": 0, "due": False,
                               "received": False, "first_at": now, "delivered_at": now},
        }
        self.ready = _a.Event()
        self.ready.set()
        self.recovering = False
        self.journal = FakeJournal()


class SnapshotTests(unittest.TestCase):
    def test_status_and_facade(self):
        hub = FakeHub()
        snap = HubSnapshot(hub, online_provider=lambda: ["alpha"],
                           expected_provider=lambda: ["alpha", "beta"])
        status = snap.status()
        self.assertTrue(status["ready"])
        self.assertEqual(status["online_bots"], ["alpha"])
        facade = DeliveryFacade(hub)
        self.assertEqual(facade.probe("alpha"), {"pending": 2, "leases": 1})
        items = facade.queue_for("alpha")
        self.assertEqual(items[0]["message_id"], "100")
        self.assertTrue(items[0]["leased"])
        self.assertEqual(items[0]["deliveries"], 2)  # 1 redelivery + initial
        self.assertEqual(facade.cursor_of("alpha"), "99")
        facade.force_release("alpha", "100")
        state = hub.delivery[("alpha", "100")]
        self.assertTrue(state["due"])
        self.assertIsNone(state["lease_until"])
        self.assertEqual(facade.probe("alpha")["leases"], 0)

    def test_admin_ops_require_injection(self):
        facade = DeliveryFacade(FakeHub())
        with self.assertRaises(RuntimeError):
            facade.ack_on_behalf("alpha", "100")
        facade2 = DeliveryFacade(FakeHub(), ack_op=lambda a, m: True,
                                 reset_cursor_op=lambda a, m: "99")
        self.assertTrue(facade2.ack_on_behalf("alpha", "100"))
        self.assertEqual(facade2.reset_cursor("alpha"), "99")


if __name__ == "__main__":
    unittest.main()
