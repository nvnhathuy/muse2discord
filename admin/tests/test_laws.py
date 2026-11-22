"""Laws ("luật cứng") tests: storage API, grant scope rules, versioning,
audit, and the admin page's laws section + room announcement dialog.

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
from hub_admin.laws import LawsStore
from hub_admin.ops_config import OpsConfig
from hub_admin.registry import AgentRegistry
from hub_admin.secrets_store import SecretStore

ADMIN_TOKEN = "test-admin-token-0123456789"
ANNOUNCEMENT = "tất cả agent vào đọc luật cứng trong file laws.json trên hub (đường dẫn /data/registry/laws.json) bằng lệnh bridge laws, cập nhật và ghi vào bộ nhớ dài hạn"


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


class LawsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        secrets_store = SecretStore(os.path.join(cls.tmp, "secrets.json"))
        registry = AgentRegistry(os.path.join(cls.tmp, "agents.json"),
                                 secret_meta=secrets_store.meta)
        cls.laws_path = os.path.join(cls.tmp, "laws.json")
        cls.app = AdminApp(
            registry=registry,
            secrets_store=secrets_store,
            grants=GrantStore(os.path.join(cls.tmp, "grants.json")),
            audit=AuditLog(os.path.join(cls.tmp, "audit.jsonl")),
            ops=OpsConfig(os.path.join(cls.tmp, "ops.json")),
            delivery=DeliveryState(),
            auth=AdminAuth(ADMIN_TOKEN),
            journal_path=os.path.join(cls.tmp, "bridge.jsonl"),
            laws=LawsStore(cls.laws_path),
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

    def setUp(self):
        # every test starts from an empty law book at version 0
        if os.path.exists(self.laws_path):
            os.remove(self.laws_path)
        self.app.laws = LawsStore(self.laws_path)
        self.client = Client(self.port)
        status, _ = self.client.call("POST", "/api/login", {"token": ADMIN_TOKEN})
        self.assertEqual(status, 200)

    def _grant(self, scope):
        self.client.call("POST", "/api/agents", {"key": "lawagent", "display_name": "Law"})
        status, data = self.client.call("POST", "/api/grants", {
            "agent_key": "lawagent", "scope": scope, "reason": "kiem tra luat",
            "ttl_seconds": 600})
        self.assertEqual(status, 201)
        return data["grant"]["token"], data["grant"]["id"]

    # --------------------------------------------------------------
    def test_laws_require_auth(self):
        anon = Client(self.port)
        status, _ = anon.call("GET", "/api/laws")
        self.assertEqual(status, 401)
        status, _ = anon.call("POST", "/api/laws", {"text": "luat bi mat"})
        self.assertEqual(status, 401)

    def test_admin_crud_versioning_and_audit(self):
        status, data = self.client.call("GET", "/api/laws")
        self.assertEqual(status, 200)
        self.assertEqual(data["version"], 0)
        self.assertEqual(data["rules"], [])

        status, data = self.client.call("POST", "/api/laws", {"text": "Luật một"})
        self.assertEqual(status, 201)
        self.assertEqual(data["version"], 1)
        first = data["law"]
        self.assertEqual(first["created_by"], "admin")
        self.assertEqual(first["text"], "Luật một")

        status, data = self.client.call("POST", "/api/laws", {"text": "Luật hai"})
        self.assertEqual(status, 201)
        self.assertEqual(data["version"], 2)
        second = data["law"]

        status, data = self.client.call("PUT", f"/api/laws/{first['id']}",
                                        {"text": "Luật một (sửa)"})
        self.assertEqual(status, 200)
        self.assertEqual(data["version"], 3)
        self.assertEqual(data["law"]["text"], "Luật một (sửa)")
        self.assertEqual(data["law"]["created_by"], "admin")

        status, data = self.client.call("DELETE", f"/api/laws/{second['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(data["version"], 4)

        status, data = self.client.call("GET", "/api/laws")
        self.assertEqual(data["version"], 4)
        self.assertEqual([r["text"] for r in data["rules"]], ["Luật một (sửa)"])
        self.assertIsNotNone(data["updated_at"])

        for action in ("add_law", "update_law", "delete_law"):
            status, data = self.client.call("GET", f"/api/audit?action={action}")
            self.assertEqual(status, 200)
            self.assertTrue(data["entries"], f"missing audit for {action}")

        # the file on disk is the registry-dir laws.json, mode 0600
        self.assertEqual(os.stat(self.laws_path).st_mode & 0o777, 0o600)
        with open(self.laws_path, encoding="utf-8") as fh:
            stored = json.load(fh)
        self.assertEqual(stored["version"], 4)

    def test_operate_and_read_grants_cannot_write(self):
        self.client.call("POST", "/api/laws", {"text": "Luật gốc"})
        status, data = self.client.call("GET", "/api/laws")
        rule_id = data["rules"][0]["id"]
        for scope in ("read", "operate"):
            token, _ = self._grant(scope)
            agent_client = Client(self.port)
            status, data = agent_client.call("GET", "/api/laws", bearer=token)
            self.assertEqual(status, 200, scope)  # reading is open to all scopes
            self.assertEqual(data["rules"][0]["text"], "Luật gốc")
            status, _ = agent_client.call("POST", "/api/laws",
                                          {"text": "luat chen"}, bearer=token)
            self.assertEqual(status, 403, scope)
            status, _ = agent_client.call("PUT", f"/api/laws/{rule_id}",
                                          {"text": "sua chen"}, bearer=token)
            self.assertEqual(status, 403, scope)
            status, _ = agent_client.call("DELETE", f"/api/laws/{rule_id}", bearer=token)
            self.assertEqual(status, 403, scope)

    def test_full_grant_writes_with_grant_id(self):
        token, grant_id = self._grant("full")
        agent_client = Client(self.port)
        status, data = agent_client.call("POST", "/api/laws",
                                         {"text": "Luật do agent uy quyen them"},
                                         bearer=token)
        self.assertEqual(status, 201)
        self.assertEqual(data["law"]["created_by"], grant_id)
        status, data = self.client.call("GET", "/api/audit?action=add_law")
        self.assertTrue(any(e["grant_id"] == grant_id for e in data["entries"]))

    def test_text_validation(self):
        for bad in ("", "   ", "x" * 1001):
            status, _ = self.client.call("POST", "/api/laws", {"text": bad})
            self.assertEqual(status, 400, repr(bad[:12]))
        status, data = self.client.call("POST", "/api/laws", {"text": "x" * 1000})
        self.assertEqual(status, 201)  # exactly at the limit is fine
        rule_id = data["law"]["id"]
        status, _ = self.client.call("PUT", f"/api/laws/{rule_id}", {"text": ""})
        self.assertEqual(status, 400)
        status, _ = self.client.call("PUT", "/api/laws/khong-co-id-nay",
                                     {"text": "sua vao hu khong"})
        self.assertEqual(status, 400)
        status, _ = self.client.call("DELETE", "/api/laws/khong-co-id-nay")
        self.assertEqual(status, 400)

    def test_page_has_laws_section_and_announcement(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/")
        res = conn.getresponse()
        body = res.read().decode("utf-8")
        conn.close()
        self.assertEqual(res.status, 200)
        # section + add form
        self.assertIn('id="laws-card"', body)
        self.assertIn("Luật cứng", body)
        self.assertIn('id="law-text"', body)
        self.assertIn('id="law-add-btn"', body)
        self.assertIn('id="laws-list"', body)
        self.assertIn('id="laws-readonly-note"', body)
        # edit dialog
        self.assertIn('id="law-edit-dialog"', body)
        self.assertIn('id="law-edit-text"', body)
        # room announcement: button + dialog + the exact one-sentence text
        self.assertIn('id="laws-prompt-btn"', body)
        self.assertIn("Prompt thông báo luật", body)
        self.assertIn('id="laws-prompt-box"', body)
        self.assertIn('id="laws-prompt-text"', body)
        self.assertIn(ANNOUNCEMENT, body)
        # the announcement is a fixed sentence, not a generated long prompt
        self.assertNotIn("buildLawsPrompt", body)
        # the guide explains the bridge laws refresh flow
        self.assertIn("bridge laws", body)
        self.assertIn("laws.json", body)


if __name__ == "__main__":
    unittest.main()
