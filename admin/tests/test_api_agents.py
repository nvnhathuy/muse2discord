"""API-agent tests (issue #22, Đơn A): kind on the profile, the
api_agents store + key files, sensitive-op rules for provider keys,
overview decoration, and deletion cleanup.

Run: python3 -m unittest discover -s tests -v
"""

import http.client
import json
import os
import stat
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hub_admin.api import AdminApp, serve
from hub_admin.audit import AuditLog
from hub_admin.auth import AdminAuth
from hub_admin.delivery import DeliveryState
from hub_admin.grants import GrantStore
from hub_admin.ops_config import OpsConfig
from hub_admin.registry import AgentRegistry, RegistryError
from hub_admin.secrets_store import SecretStore

ADMIN_TOKEN = "-".join(["test", "admin", "token"])
SAMPLE_KEY = "-".join(["provider", "key", "first", "1111"])
SAMPLE_KEY_2 = "-".join(["provider", "key", "second", "2222"])
STEP_UP = {"X-Admin-Reauth": ADMIN_TOKEN}


class Client:
    def __init__(self, port):
        self.port = port
        self.cookie = None

    def call(self, method, path, body=None, headers=None, bearer=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body)
            hdrs["Content-Type"] = "application/json"
        if self.cookie:
            hdrs["Cookie"] = self.cookie
        if bearer:
            hdrs["Authorization"] = "Bearer " + bearer
        conn.request(method, path, payload, hdrs)
        res = conn.getresponse()
        raw = res.read().decode("utf-8")
        set_cookie = res.getheader("Set-Cookie")
        if set_cookie:
            self.cookie = set_cookie.split(";")[0]
        conn.close()
        return res.status, json.loads(raw) if raw else {}


class ApiAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.secrets_store = SecretStore(os.path.join(cls.tmp, "secrets.json"))
        cls.registry = AgentRegistry(os.path.join(cls.tmp, "agents.json"),
                                     secret_meta=cls.secrets_store.meta)
        cls.audit_path = os.path.join(cls.tmp, "audit.jsonl")
        cls.app = AdminApp(
            registry=cls.registry,
            secrets_store=cls.secrets_store,
            grants=GrantStore(os.path.join(cls.tmp, "grants.json")),
            audit=AuditLog(cls.audit_path),
            ops=OpsConfig(os.path.join(cls.tmp, "ops.json")),
            delivery=DeliveryState(),
            auth=AdminAuth(ADMIN_TOKEN),
            journal_path=os.path.join(cls.tmp, "bridge.jsonl"),
            status_provider=lambda: {"ready": True, "recovering": False,
                                     "journal_healthy": True,
                                     "online_bots": [], "expected_bots": []},
        )
        cls.server = serve(cls.app, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    _counter = 0

    def setUp(self):
        self.client = Client(self.port)
        status, _ = self.client.call("POST", "/api/login", {"token": ADMIN_TOKEN})
        self.assertEqual(status, 200)
        type(self)._counter += 1
        self.key = f"apiagent{type(self)._counter}"

    # --------------------------------------------------------------
    def _create_api(self, key=None, headers=STEP_UP, **over):
        body = {"key": key or self.key, "display_name": "Api Agent",
                "kind": "api", "provider": "gemini", "api_key": SAMPLE_KEY,
                "models": ["model-a", "model-b"]}
        body.update(over)
        return self.client.call("POST", "/api/agents", body, headers=headers)

    def _key_file(self, key):
        return os.path.join(self.tmp, "api-keys", key + ".key")

    def _audit_text(self):
        with open(self.audit_path, encoding="utf-8") as fh:
            return fh.read()

    # --------------------------------------------------------------
    def test_create_api_agent_requires_key(self):
        status, _ = self.client.call(
            "POST", "/api/agents",
            {"key": self.key, "display_name": "Api", "kind": "api",
             "provider": "gemini", "models": []},
            headers=STEP_UP)
        self.assertEqual(status, 400)
        # a rejected create leaves no profile behind
        status, _ = self.client.call("GET", f"/api/agents/{self.key}")
        self.assertEqual(status, 400)

    def test_create_api_agent_requires_step_up(self):
        status, _ = self._create_api(headers=None)
        self.assertEqual(status, 403)
        status, _ = self.client.call("GET", f"/api/agents/{self.key}")
        self.assertEqual(status, 400)

    def test_create_api_agent_bad_provider(self):
        status, _ = self._create_api(provider="bogus-provider")
        self.assertEqual(status, 400)

    def test_create_api_agent_ok(self):
        status, data = self._create_api()
        self.assertEqual(status, 201)
        agent = data["agent"]
        self.assertEqual(agent["kind"], "api")
        self.assertEqual(agent["seat_type"], "api")
        self.assertTrue(agent["locked"])       # locked by default
        self.assertFalse(agent["enabled"])     # starts paused
        self.assertEqual(agent["api"]["provider"], "gemini")
        self.assertEqual(agent["api"]["models"], ["model-a", "model-b"])
        self.assertTrue(agent["api"]["has_key"])
        # key file: exact content, mode 0600
        path = self._key_file(self.key)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), SAMPLE_KEY)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        # config file: mode 0600, holds provider/models, never the key
        cfg_path = os.path.join(self.tmp, "api_agents.json")
        self.assertEqual(stat.S_IMODE(os.stat(cfg_path).st_mode), 0o600)
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        self.assertEqual(cfg["version"], 1)
        self.assertEqual(cfg["agents"][self.key]["provider"], "gemini")
        self.assertNotIn(SAMPLE_KEY, json.dumps(cfg))
        # the key appears NOWHERE in API responses
        for path_ in ("/api/overview", "/api/agents", f"/api/agents/{self.key}"):
            status, payload = self.client.call("GET", path_)
            self.assertEqual(status, 200)
            self.assertNotIn(SAMPLE_KEY, json.dumps(payload))
        # audit records the event with provider + model count only
        audit = self._audit_text()
        self.assertIn("set_api_config", audit)
        self.assertNotIn(SAMPLE_KEY, audit)

    def test_create_api_agent_default_models_gemini(self):
        status, data = self._create_api(models=[])
        self.assertEqual(status, 201)
        self.assertEqual(len(data["agent"]["api"]["models"]), 4)

    def test_set_api_key_flow(self):
        status, _ = self._create_api()
        self.assertEqual(status, 201)
        # no step-up -> 403
        status, _ = self.client.call("POST", f"/api/agents/{self.key}/api-key",
                                     {"api_key": SAMPLE_KEY_2})
        self.assertEqual(status, 403)
        # with step-up -> 200 and the file changes
        status, _ = self.client.call("POST", f"/api/agents/{self.key}/api-key",
                                     {"api_key": SAMPLE_KEY_2}, headers=STEP_UP)
        self.assertEqual(status, 200)
        with open(self._key_file(self.key), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), SAMPLE_KEY_2)
        audit = self._audit_text()
        self.assertIn("set_api_key", audit)
        self.assertNotIn(SAMPLE_KEY_2, audit)

    def test_set_api_key_grants(self):
        status, _ = self._create_api()
        self.assertEqual(status, 201)
        # an operate grant for another agent cannot touch the key
        self.client.call("POST", "/api/agents",
                         {"key": "helper", "display_name": "Helper"})
        _, g = self.client.call("POST", "/api/grants", {
            "agent_key": "helper", "scope": "operate",
            "reason": "test", "ttl_seconds": 600})
        operate_token = g["grant"]["token"]
        anon = Client(self.port)  # no admin cookie: the grant must speak for itself
        status, _ = anon.call("POST", f"/api/agents/{self.key}/api-key",
                              {"api_key": SAMPLE_KEY_2}, bearer=operate_token)
        self.assertEqual(status, 403)
        # a full grant can (it is admin-equivalent, step-up included)
        _, g = self.client.call("POST", "/api/grants", {
            "agent_key": "helper", "scope": "full",
            "reason": "test", "ttl_seconds": 600})
        full_token = g["grant"]["token"]
        status, _ = anon.call("POST", f"/api/agents/{self.key}/api-key",
                              {"api_key": SAMPLE_KEY_2}, bearer=full_token)
        self.assertEqual(status, 200)

    def test_set_api_key_on_ssh_agent_is_400(self):
        self.client.call("POST", "/api/agents",
                         {"key": "sshagent", "display_name": "Ssh"})
        status, _ = self.client.call("POST", "/api/agents/sshagent/api-key",
                                     {"api_key": SAMPLE_KEY}, headers=STEP_UP)
        self.assertEqual(status, 400)

    def test_delete_api_agent_removes_config_and_key(self):
        status, _ = self._create_api()
        self.assertEqual(status, 201)
        status, _ = self.client.call(
            "DELETE", f"/api/agents/{self.key}",
            {"confirm_name": self.key}, headers=STEP_UP)
        self.assertEqual(status, 200)
        self.assertFalse(os.path.exists(self._key_file(self.key)))
        with open(os.path.join(self.tmp, "api_agents.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        self.assertNotIn(self.key, cfg["agents"])
        self.assertIn("delete_api_config", self._audit_text())

    def test_ssh_agent_unchanged(self):
        status, data = self.client.call(
            "POST", "/api/agents", {"key": "plainssh", "display_name": "Plain"})
        self.assertEqual(status, 201)
        self.assertEqual(data["agent"]["kind"], "ssh")
        self.assertNotIn("api", data["agent"])


class RegistryKindTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "agents.json")

    def test_old_records_derive_kind_from_seat(self):
        payload = {"agents": {
            "oldapi": {"key": "oldapi", "display_name": "Old Api",
                       "seat_type": "api"},
            "oldlive": {"key": "oldlive", "display_name": "Old Live",
                        "seat_type": "live"},
        }}
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        registry = AgentRegistry(self.path)
        self.assertEqual(registry.get("oldapi").kind, "api")
        self.assertEqual(registry.get("oldlive").kind, "ssh")
        self.assertEqual(registry.public_get("oldapi")["kind"], "api")

    def test_create_kind_validation(self):
        registry = AgentRegistry(self.path)
        with self.assertRaises(RegistryError):
            registry.create({"key": "x1", "display_name": "X", "kind": "bogus"})
        with self.assertRaises(RegistryError):
            registry.create({"key": "x2", "display_name": "X",
                             "kind": "api", "seat_type": "live"})
        profile = registry.create({"key": "x3", "display_name": "X",
                                   "seat_type": "api"})
        self.assertEqual(profile.kind, "api")


if __name__ == "__main__":
    unittest.main()
