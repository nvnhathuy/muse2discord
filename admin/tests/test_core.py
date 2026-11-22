"""Core-model tests: registry, secrets, grants, ops config, delivery,
journal, audit. Run: python3 -m unittest discover -s tests -v"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hub_admin.audit import AuditLog
from hub_admin.delivery import DeliveryState
from hub_admin.grants import GrantStore, SENSITIVE_OPS, scope_allows
from hub_admin.journal import retention_report
from hub_admin.ops_config import OpsConfig
from hub_admin.registry import AgentRegistry, RegistryError
from hub_admin.secrets_store import SecretStore


def make_registry(tmp):
    secrets = SecretStore(os.path.join(tmp, "secrets.json"))
    registry = AgentRegistry(os.path.join(tmp, "agents.json"), secret_meta=secrets.meta)
    return registry, secrets


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.registry, self.secrets = make_registry(self.tmp)

    def create(self, key="alpha", **kw):
        data = {"key": key, "display_name": "Alpha", "call_tokens": ["alpha", "al"]}
        data.update(kw)
        return self.registry.create(data)

    def test_create_starts_paused(self):
        profile = self.create()
        self.assertFalse(profile.enabled)
        self.assertEqual(self.registry.routing_table(), {})

    def test_reserved_key_rejected(self):
        with self.assertRaises(RegistryError):
            self.create(key="hub")

    def test_duplicate_key_rejected(self):
        self.create()
        with self.assertRaises(RegistryError):
            self.create()

    def test_duplicate_call_token_rejected(self):
        self.create()
        with self.assertRaises(RegistryError):
            self.create(key="beta", call_tokens=["AL"])  # case-insensitive

    def test_deadline_floor(self):
        with self.assertRaises(RegistryError):
            self.create(receive_deadline=60)

    def test_key_immutable_on_update(self):
        self.create()
        profile = self.registry.update("alpha", {"key": "beta", "role": "lead"})
        self.assertEqual(profile.key, "alpha")
        self.assertEqual(profile.role, "lead")

    def test_uneditable_field_rejected(self):
        self.create()
        with self.assertRaises(RegistryError):
            self.registry.update("alpha", {"enabled": True})

    def test_pause_resume_hot(self):
        self.create()
        self.registry.set_enabled("alpha", True, by="admin")
        self.assertIn("alpha", self.registry.routing_table())
        paused = self.registry.set_enabled("alpha", False, by="admin", reason="loi")
        self.assertEqual(paused.pause_reason, "loi")
        self.assertEqual(self.registry.routing_table(), {})

    def test_persistence_across_instances(self):
        self.create()
        again, _ = make_registry(self.tmp)
        self.assertEqual([p.key for p in again.list_agents()], ["alpha"])

    def test_seed_from_env_only_once(self):
        env = {
            "AGENT_KEYS": "alpha,beta",
            "ALPHA_DISPLAY_NAME": "Alpha",
            "ALPHA_CALL_TOKENS": '["alpha"]',
            "BETA_DISPLAY_NAME": "Beta",
            "BETA_RECEIVE_DEADLINE": "300",
        }
        imported = self.registry.seed_from_env(env)
        self.assertEqual(imported, ["alpha", "beta"])
        self.assertTrue(self.registry.get("beta").enabled)
        self.assertEqual(self.registry.get("beta").receive_deadline, 300)
        self.assertEqual(self.registry.seed_from_env(env), [])

    def test_token_never_in_public_dict(self):
        self.create()
        self.secrets.set("alpha", "super-secret-token-value")
        public = self.registry.public_get("alpha")
        self.assertTrue(public["token_configured"])
        self.assertEqual(public["token_length"], len("super-secret-token-value"))
        self.assertNotIn("super-secret-token-value", json.dumps(public))

    def test_hard_delete_guard_and_success(self):
        self.create()
        probe_busy = lambda key: {"pending": 2, "leases": 0}
        with self.assertRaises(RegistryError):
            self.registry.hard_delete("alpha", probe_busy)
        probe_free = lambda key: {"pending": 0, "leases": 0}
        self.registry.hard_delete("alpha", probe_free)
        self.assertEqual(self.registry.list_agents(), [])


class GrantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.grants = GrantStore(os.path.join(self.tmp, "grants.json"))

    def test_issue_and_authenticate(self):
        grant, token = self.grants.issue("alpha", "read", "kiem tra", ttl_seconds=600)
        self.assertIsNotNone(self.grants.authenticate(token))
        self.assertIsNone(self.grants.authenticate("wrong-token"))
        # token plaintext is never persisted
        with open(os.path.join(self.tmp, "grants.json")) as fh:
            self.assertNotIn(token, fh.read())

    def test_revoke_is_immediate(self):
        grant, token = self.grants.issue("alpha", "operate", "ho tro", ttl_seconds=600)
        self.grants.revoke(grant.id)
        self.assertIsNone(self.grants.authenticate(token))

    def test_expiry(self):
        grant, token = self.grants.issue("alpha", "read", "ngan", ttl_seconds=1)
        time.sleep(1.1)
        self.assertIsNone(self.grants.authenticate(token))

    def test_ttl_bounds_and_reason_required(self):
        with self.assertRaises(ValueError):
            self.grants.issue("alpha", "read", "x", ttl_seconds=25 * 3600)
        with self.assertRaises(ValueError):
            self.grants.issue("alpha", "read", "", ttl_seconds=60)
        with self.assertRaises(ValueError):
            self.grants.issue("alpha", "admin", "x", ttl_seconds=60)

    def test_sensitive_ops_never_grantable(self):
        for op in SENSITIVE_OPS:
            self.assertFalse(scope_allows("operate", op), op)
            self.assertFalse(scope_allows("read", op), op)
        self.assertTrue(scope_allows("read", "view"))
        self.assertFalse(scope_allows("read", "pause_agent"))
        self.assertTrue(scope_allows("operate", "pause_agent"))


class OpsConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ops = OpsConfig(os.path.join(self.tmp, "ops.json"))

    def test_defaults_match_live_hub(self):
        cfg = self.ops.get_all()
        self.assertEqual(cfg["lease_base_seconds"], 300)
        self.assertEqual(cfg["lease_max_seconds"], 1800)
        self.assertEqual(cfg["redelivery_max"], 3)
        self.assertEqual(cfg["circuit_breaker_peer_messages"], 8)

    def test_update_diff_and_validation(self):
        diff = self.ops.update({"redelivery_max": 5})
        self.assertEqual(diff, {"redelivery_max": (3, 5)})
        with self.assertRaises(ValueError):
            self.ops.update({"nope": 1})
        with self.assertRaises(ValueError):
            self.ops.update({"lease_base_seconds": 99999})


class DeliveryTests(unittest.TestCase):
    def test_queue_lease_ack_cycle(self):
        state = DeliveryState()
        state.enqueue("alpha", "m1", author="owner")
        state.enqueue("alpha", "m2")
        self.assertEqual(state.probe("alpha"), {"pending": 2, "leases": 0})
        record = state.claim_lease("alpha", "m1")
        self.assertEqual(record["deliveries"], 1)
        self.assertEqual(state.probe("alpha")["leases"], 1)
        state.force_release("alpha", "m1")
        self.assertEqual(state.probe("alpha")["leases"], 0)
        self.assertTrue(state.ack("alpha", "m1"))
        self.assertEqual(state.cursor_of("alpha"), "m1")
        previous = state.reset_cursor("alpha")
        self.assertEqual(previous, "m1")
        self.assertEqual(state.cursor_of("alpha"), "0")


class JournalAuditTests(unittest.TestCase):
    def test_retention_report(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "bridge.jsonl")
        with open(path, "w") as fh:
            fh.write(json.dumps({"ts": 1000.0}) + "\n")
            fh.write(json.dumps({"ts": 1000.0 + 100 * 86400}) + "\n")
        report = retention_report(path, online_days=90, rotate_mb=50)
        self.assertEqual(report["lines"], 2)
        self.assertTrue(report["over_online_window"])
        self.assertTrue(report["action_needed"])

    def test_audit_append_and_filter(self):
        tmp = tempfile.mkdtemp()
        log = AuditLog(os.path.join(tmp, "audit.jsonl"))
        log.record("admin", "pause_agent", "alpha", {"reason": "loi"})
        log.record("alpha", "resume_agent", "alpha", {}, grant_id="g1")
        self.assertEqual(len(log.query(agent="alpha")), 2)
        self.assertEqual(len(log.query(action="pause_agent")), 1)
        self.assertEqual(log.query()[-1]["grant_id"], "g1")


if __name__ == "__main__":
    unittest.main()
