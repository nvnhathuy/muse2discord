"""TOTP tests: RFC 6238 vectors + the admin step-up flow end to end.

Run: python3 -m unittest discover -s tests_integration -v
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "admin"))

from hub_admin.api import AdminApp, serve
from hub_admin.audit import AuditLog
from hub_admin.auth import AdminAuth
from hub_admin.credentials import AdminCredentials
from hub_admin.delivery import DeliveryState
from hub_admin.grants import GrantStore
from hub_admin.ops_config import OpsConfig
from hub_admin.registry import AgentRegistry
from hub_admin.secrets_store import SecretStore
from hub_admin.totp import generate_secret, otpauth_uri, totp_now, verify_totp

PW = "admin-password-1"


class TotpUnitTests(unittest.TestCase):
    def test_rfc6238_sha1_vectors(self):
        # RFC 6238 Appendix B test seed (ASCII "12345678901234567890")
        import base64
        secret = base64.b32encode(b"12345678901234567890").decode()
        self.assertEqual(totp_now(secret, at=59), "287082")
        self.assertEqual(totp_now(secret, at=1111111109), "081804")
        self.assertEqual(totp_now(secret, at=1234567890), "005924")

    def test_verify_window_and_format(self):
        secret = generate_secret()
        code = totp_now(secret)
        self.assertTrue(verify_totp(secret, code))
        self.assertFalse(verify_totp(secret, "000000" if code != "000000" else "111111"))
        self.assertFalse(verify_totp(secret, "abcdef"))
        self.assertFalse(verify_totp(secret, "12345"))
        self.assertIn("otpauth://totp/", otpauth_uri(secret))


class TotpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.creds = AdminCredentials(os.path.join(cls.tmp, "admin_credentials.json"))
        cls.creds.set_password(PW)

        def step_up(presented):
            secret = cls.creds.totp_secret()
            if secret:
                return verify_totp(secret, presented)
            return cls.creds.verify(presented)

        secrets_store = SecretStore(os.path.join(cls.tmp, "secrets.json"))
        cls.app = AdminApp(
            registry=AgentRegistry(os.path.join(cls.tmp, "agents.json"), secret_meta=secrets_store.meta),
            secrets_store=secrets_store,
            grants=GrantStore(os.path.join(cls.tmp, "grants.json")),
            audit=AuditLog(os.path.join(cls.tmp, "audit.jsonl")),
            ops=OpsConfig(os.path.join(cls.tmp, "ops.json")),
            delivery=DeliveryState(),
            auth=AdminAuth(cls.creds.verify, version_provider=cls.creds.version,
                           step_up_verifier=step_up),
            journal_path=os.path.join(cls.tmp, "bridge.jsonl"),
            credentials=cls.creds,
        )
        cls.server = serve(cls.app, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def call(self, method, path, body=None, headers=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body)
            hdrs["Content-Type"] = "application/json"
        if cookie:
            hdrs["Cookie"] = cookie
        conn.request(method, path, payload, hdrs)
        res = conn.getresponse()
        raw = res.read().decode("utf-8")
        set_cookie = res.getheader("Set-Cookie")
        conn.close()
        return res.status, (json.loads(raw) if raw else {}), (set_cookie.split(";")[0] if set_cookie else cookie)

    def login(self):
        status, _, cookie = self.call("POST", "/api/login", {"password": PW})
        self.assertEqual(status, 200)
        return cookie

    def test_enroll_then_totp_step_up(self):
        cookie = self.login()
        # before enrollment: password step-up works for a sensitive op
        self.app.registry.create({"key": "alpha", "display_name": "Alpha"})
        status, _, _ = self.call("POST", "/api/agents/alpha/token", {"bot_token": "tok-1"},
                                 headers={"X-Admin-Reauth": PW}, cookie=cookie)
        self.assertEqual(status, 200)
        # enroll needs the current password
        status, _, _ = self.call("POST", "/api/totp/enroll", {"current_password": "sai"}, cookie=cookie)
        self.assertEqual(status, 403)
        status, data, _ = self.call("POST", "/api/totp/enroll", {"current_password": PW}, cookie=cookie)
        self.assertEqual(status, 201)
        secret = data["totp_secret"]
        # after enrollment the PASSWORD no longer works as step-up...
        status, _, _ = self.call("POST", "/api/agents/alpha/token", {"bot_token": "tok-2"},
                                 headers={"X-Admin-Reauth": PW}, cookie=cookie)
        self.assertEqual(status, 403)
        # ...but the current TOTP code does
        status, _, _ = self.call("POST", "/api/agents/alpha/token", {"bot_token": "tok-2"},
                                 headers={"X-Admin-Reauth": totp_now(secret)}, cookie=cookie)
        self.assertEqual(status, 200)
        # overview reports totp_enabled
        status, data, _ = self.call("GET", "/api/overview", cookie=cookie)
        self.assertTrue(data["identity"]["totp_enabled"])
        # disable restores password step-up
        status, _, _ = self.call("POST", "/api/totp/disable", {"current_password": PW}, cookie=cookie)
        self.assertEqual(status, 200)
        status, _, _ = self.call("POST", "/api/agents/alpha/token", {"bot_token": "tok-3"},
                                 headers={"X-Admin-Reauth": PW}, cookie=cookie)
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
