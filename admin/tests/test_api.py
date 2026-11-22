"""End-to-end API tests against the real HTTP server (ephemeral port).

Covers: login/session, agent lifecycle, pause-reason rule, grant scopes
(read vs operate vs sensitive), step-up for sensitive ops, hard-delete
guard, ops-config step-up, and audit entries with grant ids.
Run: python3 -m unittest discover -s tests -v
"""

import http.client
import json
import os
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
from hub_admin.registry import AgentRegistry
from hub_admin.secrets_store import SecretStore

ADMIN_TOKEN = "test-admin-token-0123456789"


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


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        secrets_store = SecretStore(os.path.join(cls.tmp, "secrets.json"))
        registry = AgentRegistry(os.path.join(cls.tmp, "agents.json"), secret_meta=secrets_store.meta)
        cls.app = AdminApp(
            registry=registry,
            secrets_store=secrets_store,
            grants=GrantStore(os.path.join(cls.tmp, "grants.json")),
            audit=AuditLog(os.path.join(cls.tmp, "audit.jsonl")),
            ops=OpsConfig(os.path.join(cls.tmp, "ops.json")),
            delivery=DeliveryState(),
            auth=AdminAuth(ADMIN_TOKEN),
            journal_path=os.path.join(cls.tmp, "bridge.jsonl"),
            status_provider=lambda: {"ready": True, "recovering": False,
                                     "journal_healthy": True,
                                     "online_bots": ["alpha"], "expected_bots": ["alpha"]},
        )
        cls.server = serve(cls.app, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.client = Client(self.port)
        status, _ = self.client.call("POST", "/api/login", {"token": ADMIN_TOKEN})
        self.assertEqual(status, 200)

    # --------------------------------------------------------------
    def test_auth_required(self):
        anon = Client(self.port)
        status, _ = anon.call("GET", "/api/agents")
        self.assertEqual(status, 401)
        status, _ = anon.call("POST", "/api/login", {"token": "wrong"})
        self.assertEqual(status, 401)

    def test_logout_kills_session(self):
        status, _ = self.client.call("GET", "/api/agents")
        self.assertEqual(status, 200)
        status, _ = self.client.call("POST", "/api/logout")
        self.assertEqual(status, 200)
        status, _ = self.client.call("GET", "/api/agents")
        self.assertEqual(status, 401)
        # a fresh login still works after logout
        status, _ = self.client.call("POST", "/api/login", {"token": ADMIN_TOKEN})
        self.assertEqual(status, 200)
        status, _ = self.client.call("GET", "/api/agents")
        self.assertEqual(status, 200)

    def test_agent_lifecycle_and_secrets_hidden(self):
        status, data = self.client.call("POST", "/api/agents", {
            "key": "alpha", "display_name": "Alpha",
            "call_tokens": ["alpha"], "bot_token": "tok-value-123",
        })
        self.assertEqual(status, 201)
        self.assertFalse(data["agent"]["enabled"])  # starts paused
        self.assertTrue(data["agent"]["token_configured"])
        self.assertNotIn("tok-value-123", json.dumps(data))
        status, data = self.client.call("GET", "/api/overview")
        self.assertEqual(status, 200)
        self.assertEqual(data["agents"][0]["key"], "alpha")
        self.assertTrue(data["agents"][0]["online"])

    def test_pause_requires_reason_then_resume(self):
        self.client.call("POST", "/api/agents", {"key": "beta", "display_name": "Beta"})
        status, _ = self.client.call("POST", "/api/agents/beta/pause", {})
        self.assertEqual(status, 400)
        status, data = self.client.call("POST", "/api/agents/beta/pause", {"reason": "loi"})
        self.assertEqual(status, 200)
        self.assertFalse(data["agent"]["enabled"])
        status, data = self.client.call("POST", "/api/agents/beta/resume", {})
        self.assertEqual(status, 200)
        self.assertTrue(data["agent"]["enabled"])

    def test_grant_scopes(self):
        self.client.call("POST", "/api/agents", {"key": "gamma", "display_name": "Gamma"})
        status, data = self.client.call("POST", "/api/grants", {
            "agent_key": "gamma", "scope": "read", "reason": "xem thu", "ttl_seconds": 600})
        self.assertEqual(status, 201)
        read_token = data["grant"]["token"]
        agent_client = Client(self.port)
        status, _ = agent_client.call("GET", "/api/overview", bearer=read_token)
        self.assertEqual(status, 200)
        status, _ = agent_client.call("POST", "/api/agents/gamma/pause",
                                      {"reason": "x"}, bearer=read_token)
        self.assertEqual(status, 403)  # read scope cannot operate

        status, data = self.client.call("POST", "/api/grants", {
            "agent_key": "gamma", "scope": "operate", "reason": "ho tro", "ttl_seconds": 600})
        operate_token = data["grant"]["token"]
        grant_id = data["grant"]["id"]
        status, _ = agent_client.call("POST", "/api/agents/gamma/pause",
                                      {"reason": "ho tro ky thuat"}, bearer=operate_token)
        self.assertEqual(status, 200)
        # sensitive ops stay forbidden for operate grants
        status, _ = agent_client.call("DELETE", "/api/agents/gamma",
                                      {"confirm_name": "gamma"}, bearer=operate_token,
                                      headers={"X-Admin-Reauth": ADMIN_TOKEN})
        self.assertEqual(status, 403)
        status, _ = agent_client.call("GET", "/api/grants", bearer=operate_token)
        self.assertEqual(status, 403)
        # the operate action was audited with the grant id
        status, data = self.client.call("GET", "/api/audit?agent=gamma")
        actions = {(e["action"], e["grant_id"]) for e in data["entries"]}
        self.assertIn(("pause_agent", grant_id), actions)
        # revoke -> immediate effect
        status, _ = self.client.call("DELETE", f"/api/grants/{grant_id}")
        self.assertEqual(status, 200)
        status, _ = agent_client.call("GET", "/api/overview", bearer=operate_token)
        self.assertEqual(status, 401)

    def test_full_grant_is_admin_equivalent_but_cannot_issue_grants(self):
        # Owner decisions 2026-11-22 09:50 + 09:51: a full temporary
        # grant does EVERYTHING the admin can (sensitive ops included)
        # EXCEPT grant administration itself - issuing stays admin-only,
        # no chained delegation, no self-renewal.
        self.client.call("POST", "/api/agents", {"key": "eps", "display_name": "Epsilon"})
        status, data = self.client.call("POST", "/api/grants", {
            "agent_key": "eps", "scope": "full", "reason": "thay admin xu ly su co",
            "ttl_seconds": 600})
        self.assertEqual(status, 201)
        full_token = data["grant"]["token"]
        full_id = data["grant"]["id"]
        agent_client = Client(self.port)
        # sensitive op 1: set bot token (no admin step-up header needed)
        status, _ = agent_client.call("POST", "/api/agents/eps/token",
                                      {"bot_token": "tok-by-full-grant"}, bearer=full_token)
        self.assertEqual(status, 200)
        # sensitive op 2: edit ops config
        status, data = agent_client.call("PATCH", "/api/ops-config",
                                         {"changes": {"redelivery_max": 6}}, bearer=full_token)
        self.assertEqual(status, 200)
        self.assertEqual(data["ops_config"]["redelivery_max"], 6)
        # the ONE exception: grant administration is admin-only
        status, _ = agent_client.call("POST", "/api/grants", {
            "agent_key": "eps", "scope": "full", "reason": "tu cap", "ttl_seconds": 600},
            bearer=full_token)
        self.assertEqual(status, 403)
        status, _ = agent_client.call("GET", "/api/grants", bearer=full_token)
        self.assertEqual(status, 403)
        status, _ = agent_client.call("DELETE", f"/api/grants/{full_id}", bearer=full_token)
        self.assertEqual(status, 403)
        # sensitive op 3: hard delete with typed confirmation
        status, _ = agent_client.call("DELETE", "/api/agents/eps",
                                      {"confirm_name": "eps"}, bearer=full_token)
        self.assertEqual(status, 200)
        # actions under the grant are audited under the agent + grant id
        status, data = self.client.call("GET", "/api/audit?agent=eps")
        grant_actions = {e["action"] for e in data["entries"] if e["grant_id"] == full_id}
        self.assertIn("set_bot_token", grant_actions)
        self.assertIn("delete_agent", grant_actions)

    def test_sensitive_ops_need_step_up(self):
        self.client.call("POST", "/api/agents", {"key": "delta", "display_name": "Delta"})
        status, _ = self.client.call("PATCH", "/api/ops-config", {"changes": {"redelivery_max": 4}})
        self.assertEqual(status, 403)  # no step-up header
        status, data = self.client.call("PATCH", "/api/ops-config",
                                        {"changes": {"redelivery_max": 4}},
                                        headers={"X-Admin-Reauth": ADMIN_TOKEN})
        self.assertEqual(status, 200)
        self.assertEqual(data["ops_config"]["redelivery_max"], 4)
        # delete: blocked while messages pending
        self.app.delivery.enqueue("delta", "m1")
        status, _ = self.client.call("DELETE", "/api/agents/delta",
                                     {"confirm_name": "delta"},
                                     headers={"X-Admin-Reauth": ADMIN_TOKEN})
        self.assertEqual(status, 400)
        self.app.delivery.ack("delta", "m1")
        # delete: wrong confirmation
        status, _ = self.client.call("DELETE", "/api/agents/delta",
                                     {"confirm_name": "sai"},
                                     headers={"X-Admin-Reauth": ADMIN_TOKEN})
        self.assertEqual(status, 400)
        status, data = self.client.call("DELETE", "/api/agents/delta",
                                        {"confirm_name": "delta"},
                                        headers={"X-Admin-Reauth": ADMIN_TOKEN})
        self.assertEqual(status, 200)
        status, _ = self.client.call("GET", "/api/agents/delta")
        self.assertEqual(status, 400)

    def test_page_served(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/")
        res = conn.getresponse()
        body = res.read().decode("utf-8")
        conn.close()
        self.assertEqual(res.status, 200)
        self.assertIn("Quản trị Hub", body)

    def test_page_has_admin_guide(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/")
        res = conn.getresponse()
        body = res.read().decode("utf-8")
        conn.close()
        self.assertEqual(res.status, 200)
        self.assertIn("Hướng dẫn quản trị", body)

    def test_login_form_is_password_manager_friendly(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/")
        res = conn.getresponse()
        body = res.read().decode("utf-8")
        conn.close()
        self.assertEqual(res.status, 200)
        self.assertIn('id="login-form"', body)
        self.assertIn('autocomplete="username"', body)
        self.assertIn('autocomplete="current-password"', body)

    def test_page_is_responsive(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/")
        res = conn.getresponse()
        body = res.read().decode("utf-8")
        conn.close()
        self.assertEqual(res.status, 200)
        self.assertIn("@media", body)
        self.assertIn("font-size: 16px", body)

    def test_page_has_connect_prompt_builder(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/")
        res = conn.getresponse()
        body = res.read().decode("utf-8")
        conn.close()
        self.assertEqual(res.status, 200)
        # (a) the prompt box, its editable textarea and the per-row button exist
        self.assertIn('id="prompt-box"', body)
        self.assertIn('id="prompt-text"', body)
        self.assertIn("buildConnectPrompt", body)
        self.assertIn("Prompt kết nối", body)
        # (b) the builder only reads non-secret fields: its source (up to the
        # next function) must never reference the bot token field, and must
        # pull the embedded sentinel template
        seg = body[body.index("function buildConnectPrompt"):
                   body.index("function showConnectPrompt")]
        self.assertNotIn("bot_token", seg)
        self.assertIn("sentinel-template", seg)
        # (c) the standard sentinel script is embedded verbatim in the page
        # (sample key "deputy" is the placeholder the builder substitutes)
        self.assertIn('id="sentinel-template"', body)
        self.assertIn("#!/bin/bash", body)
        self.assertIn("SENTINEL_MAX_LIFE", body)
        self.assertIn("--agent deputy", body)
        # (d) the generated prompt walks the agent through the SSH key step:
        # the agent sends its PUBLIC key and the admin page itself installs
        # it on the coordination VM (no intermediary)
        self.assertIn("authorized_keys", seg)
        self.assertIn("PUBLIC key", seg)
        self.assertIn("trang tự thêm", seg)


if __name__ == "__main__":
    unittest.main()
