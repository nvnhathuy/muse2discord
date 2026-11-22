"""Tests for the agent SSH-key intake (keydrop flow).

Covers: strict public-key validation, the SSH payload shape (exactly
two lines, comment stripped), fingerprint computation, transport
failure semantics (502, nothing recorded), the endpoint's auth ladder
(401 anon / 403 operate grant / step-up for admin / full grant OK),
and the SSH-key dialog being present in the served page.
Run: python3 -m unittest discover -s tests -v
"""

import base64
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
from hub_admin.keydrop import (
    KeydropClient,
    KeydropError,
    key_fingerprint,
    validate_public_key,
)
from hub_admin.ops_config import OpsConfig
from hub_admin.registry import AgentRegistry
from hub_admin.secrets_store import SecretStore

ADMIN_TOKEN = "test-admin-token-0123456789"


def make_key_line(seed: bytes = b"\x07") -> str:
    """A structurally real ssh-ed25519 public key line (test fixture)."""
    def field(data: bytes) -> bytes:
        return len(data).to_bytes(4, "big") + data
    blob = base64.b64encode(field(b"ssh-ed25519") + field(seed * 32)).decode()
    return f"ssh-ed25519 {blob}"


class ValidatePublicKeyTests(unittest.TestCase):
    def test_valid_line_and_fingerprint(self):
        line = make_key_line()
        normalized, fingerprint = validate_public_key(line + " agent@laptop")
        self.assertEqual(normalized, line)  # comment stripped
        blob = line.split()[1]
        self.assertEqual(fingerprint, key_fingerprint(blob))
        self.assertTrue(fingerprint.startswith("SHA256:"))

    def test_rejects_wrong_type(self):
        blob = make_key_line().split()[1]
        with self.assertRaises(ValueError):
            validate_public_key(f"ssh-rsa {blob}")

    def test_rejects_multiline_and_injection(self):
        line = make_key_line()
        with self.assertRaises(ValueError):
            validate_public_key(line + "\n" + line)
        with self.assertRaises(ValueError):
            validate_public_key(line + '\ncommand="evil" ' + line)

    def test_rejects_foreign_blob_and_garbage(self):
        def field(data: bytes) -> bytes:
            return len(data).to_bytes(4, "big") + data
        rsa_blob = base64.b64encode(field(b"ssh-rsa") + field(b"\x01" * 32)).decode()
        with self.assertRaises(ValueError):
            validate_public_key(f"ssh-ed25519 {rsa_blob}")
        with self.assertRaises(ValueError):
            validate_public_key("ssh-ed25519 " + "A" * 44)  # decodes, wrong shape
        with self.assertRaises(ValueError):
            validate_public_key("not a key")


class KeydropClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.calls = []
        self.rc = 0

        def runner(argv, stdin_text, timeout):
            self.calls.append((argv, stdin_text))
            return self.rc, "refused" if self.rc else ""

        def keygen(argv, stdin_text, timeout):
            path = argv[argv.index("-f") + 1]
            with open(path, "w") as fh:
                fh.write("fake-private-key")
            with open(path + ".pub", "w") as fh:
                fh.write("ssh-ed25519 fake")
            return 0, ""

        self.client = KeydropClient(
            self.tmp, target="keydrop@vm-placeholder",
            runner=runner, keygen_runner=keygen)

    def test_submit_sends_exactly_two_lines(self):
        line = make_key_line()
        fingerprint = self.client.submit("kappa", line + " agent@laptop")
        self.assertEqual(fingerprint, key_fingerprint(line.split()[1]))
        self.assertEqual(len(self.calls), 1)
        argv, stdin_text = self.calls[0]
        self.assertEqual(argv[0], "ssh")
        self.assertIn("keydrop@vm-placeholder", argv)
        self.assertEqual(stdin_text, f"kappa\n{line}\nfenced\n")  # comment stripped; new agents are locked -> fenced
        # hub keypair was generated in the state dir, private key 0600
        key_path = os.path.join(self.tmp, "hub_ssh_key")
        self.assertTrue(os.path.exists(key_path))
        self.assertEqual(os.stat(key_path).st_mode & 0o777, 0o600)

    def test_submit_transport_failure(self):
        self.rc = 1
        with self.assertRaises(KeydropError):
            self.client.submit("kappa", make_key_line())

    def test_submit_without_target(self):
        client = KeydropClient(self.tmp, target="", runner=lambda *a: (0, ""))
        with self.assertRaises(KeydropError):
            client.submit("kappa", make_key_line())

    def test_submit_rejects_bad_key_before_transport(self):
        with self.assertRaises(ValueError):
            self.client.submit("kappa", "ssh-rsa AAAA")
        self.assertEqual(self.calls, [])


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


class SshKeyApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.ssh_calls = []
        cls.ssh_rc = [0]

        def runner(argv, stdin_text, timeout):
            cls.ssh_calls.append((argv, stdin_text))
            return cls.ssh_rc[0], "" if cls.ssh_rc[0] == 0 else "intake refused"

        def keygen(argv, stdin_text, timeout):
            path = argv[argv.index("-f") + 1]
            with open(path, "w") as fh:
                fh.write("fake-private-key")
            with open(path + ".pub", "w") as fh:
                fh.write("ssh-ed25519 fake")
            return 0, ""

        keydrop = KeydropClient(
            cls.tmp, target="keydrop@vm-placeholder",
            runner=runner, keygen_runner=keygen)
        secrets_store = SecretStore(os.path.join(cls.tmp, "secrets.json"))
        registry = AgentRegistry(
            os.path.join(cls.tmp, "agents.json"), secret_meta=secrets_store.meta)
        cls.app = AdminApp(
            registry=registry,
            secrets_store=secrets_store,
            grants=GrantStore(os.path.join(cls.tmp, "grants.json")),
            audit=AuditLog(os.path.join(cls.tmp, "audit.jsonl")),
            ops=OpsConfig(os.path.join(cls.tmp, "ops.json")),
            delivery=DeliveryState(),
            auth=AdminAuth(ADMIN_TOKEN),
            journal_path=os.path.join(cls.tmp, "bridge.jsonl"),
            keydrop=keydrop,
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

    def _create(self, key):
        status, _ = self.client.call(
            "POST", "/api/agents", {"key": key, "display_name": key.title()})
        self.assertEqual(status, 201)

    def _overview_agent(self, key):
        status, data = self.client.call("GET", "/api/overview")
        self.assertEqual(status, 200)
        return next(a for a in data["agents"] if a["key"] == key)

    # --------------------------------------------------------------
    def test_ssh_key_requires_auth(self):
        anon = Client(self.port)
        status, _ = anon.call("POST", "/api/agents/kappa/ssh-key",
                              {"public_key": make_key_line()})
        self.assertEqual(status, 401)

    def test_ssh_key_admin_flow(self):
        self._create("kappa")
        line = make_key_line()
        # admin session alone is not enough: sensitive op needs step-up
        status, _ = self.client.call("POST", "/api/agents/kappa/ssh-key",
                                     {"public_key": line})
        self.assertEqual(status, 403)
        status, data = self.client.call(
            "POST", "/api/agents/kappa/ssh-key",
            {"public_key": line + " agent@laptop"},
            headers={"X-Admin-Reauth": ADMIN_TOKEN})
        self.assertEqual(status, 200)
        expected_fp = key_fingerprint(line.split()[1])
        self.assertEqual(data["fingerprint"], expected_fp)
        # the SSH payload is agent + key + mode, comment stripped;
        # a fresh agent is locked by default, so the mode is fenced
        argv, stdin_text = self.ssh_calls[-1]
        self.assertEqual(stdin_text, f"kappa\n{line}\nfenced\n")
        # registry + overview carry the stamp
        row = self._overview_agent("kappa")
        self.assertEqual(row["ssh_key_fingerprint"], expected_fp)
        self.assertIsNotNone(row["ssh_key_added_at"])
        # audited
        status, data = self.client.call("GET", "/api/audit?agent=kappa")
        self.assertIn("set_ssh_key", {e["action"] for e in data["entries"]})

    def test_ssh_key_bad_formats(self):
        self._create("eta")
        line = make_key_line()
        headers = {"X-Admin-Reauth": ADMIN_TOKEN}
        for bad in (
            "ssh-rsa " + line.split()[1],                    # not ed25519
            line + "\n" + line,                              # multi-line
            line + '\ncommand="x" ' + line,                  # authorized_keys injection
            "ssh-ed25519 " + "A" * 44,                       # blob of another type
            "",
        ):
            status, _ = self.client.call("POST", "/api/agents/eta/ssh-key",
                                         {"public_key": bad}, headers=headers)
            self.assertEqual(status, 400, f"payload {bad[:40]!r} must be 400")
        row = self._overview_agent("eta")
        self.assertEqual(row["ssh_key_fingerprint"], "")
        self.assertIsNone(row["ssh_key_added_at"])

    def test_ssh_key_operate_grant_forbidden_full_grant_ok(self):
        self._create("theta")
        status, data = self.client.call("POST", "/api/grants", {
            "agent_key": "theta", "scope": "operate",
            "reason": "ho tro", "ttl_seconds": 600})
        self.assertEqual(status, 201)
        agent_client = Client(self.port)
        status, _ = agent_client.call(
            "POST", "/api/agents/theta/ssh-key",
            {"public_key": make_key_line()}, bearer=data["grant"]["token"])
        self.assertEqual(status, 403)  # sensitive: operate never reaches it

        status, data = self.client.call("POST", "/api/grants", {
            "agent_key": "theta", "scope": "full",
            "reason": "thay admin", "ttl_seconds": 600})
        self.assertEqual(status, 201)
        status, data = agent_client.call(
            "POST", "/api/agents/theta/ssh-key",
            {"public_key": make_key_line()}, bearer=data["grant"]["token"])
        self.assertEqual(status, 200)  # full grant = admin-equivalent
        self.assertTrue(data["fingerprint"].startswith("SHA256:"))

    def test_ssh_key_transport_failure_is_502_and_records_nothing(self):
        self._create("zeta")
        self.ssh_rc[0] = 1
        try:
            status, _ = self.client.call(
                "POST", "/api/agents/zeta/ssh-key",
                {"public_key": make_key_line()},
                headers={"X-Admin-Reauth": ADMIN_TOKEN})
        finally:
            self.ssh_rc[0] = 0
        self.assertEqual(status, 502)
        row = self._overview_agent("zeta")
        self.assertEqual(row["ssh_key_fingerprint"], "")
        self.assertIsNone(row["ssh_key_added_at"])

    # --------------------------------------------------------------
    # agent lock ("khoá trong hub"): locked by default, the toggle
    # re-fences the installed key through the same keydrop channel.
    def test_new_agent_locked_by_default(self):
        self._create("locka")
        self.assertTrue(self._overview_agent("locka")["locked"])

    def test_lock_requires_auth(self):
        anon = Client(self.port)
        status, _ = anon.call("POST", "/api/agents/locka/lock", {"locked": False})
        self.assertEqual(status, 401)

    def test_lock_toggle_refences_installed_key(self):
        self._create("lockb")
        headers = {"X-Admin-Reauth": ADMIN_TOKEN}
        status, _ = self.client.call("POST", "/api/agents/lockb/ssh-key",
                                     {"public_key": make_key_line()},
                                     headers=headers)
        self.assertEqual(status, 200)
        # sensitive: the admin session alone is not enough
        status, _ = self.client.call("POST", "/api/agents/lockb/lock",
                                     {"locked": False})
        self.assertEqual(status, 403)
        # unlock with step-up: a mode-only submission re-fences the key
        status, data = self.client.call("POST", "/api/agents/lockb/lock",
                                        {"locked": False}, headers=headers)
        self.assertEqual(status, 200)
        self.assertFalse(data["agent"]["locked"])
        argv, stdin_text = self.ssh_calls[-1]
        self.assertEqual(stdin_text, "lockb\nmode:open\n")
        self.assertFalse(self._overview_agent("lockb")["locked"])
        # lock again -> fenced
        status, data = self.client.call("POST", "/api/agents/lockb/lock",
                                        {"locked": True}, headers=headers)
        self.assertEqual(status, 200)
        argv, stdin_text = self.ssh_calls[-1]
        self.assertEqual(stdin_text, "lockb\nmode:fenced\n")
        self.assertTrue(self._overview_agent("lockb")["locked"])
        # audited
        status, data = self.client.call("GET", "/api/audit?agent=lockb")
        self.assertIn("set_agent_lock", {e["action"] for e in data["entries"]})

    def test_lock_without_key_skips_keydrop(self):
        self._create("lockc")
        before = len(self.ssh_calls)
        status, data = self.client.call(
            "POST", "/api/agents/lockc/lock", {"locked": False},
            headers={"X-Admin-Reauth": ADMIN_TOKEN})
        self.assertEqual(status, 200)
        self.assertFalse(data["agent"]["locked"])
        self.assertEqual(len(self.ssh_calls), before)  # nothing shipped

    def test_lock_operate_grant_forbidden(self):
        self._create("lockd")
        status, data = self.client.call("POST", "/api/grants", {
            "agent_key": "lockd", "scope": "operate",
            "reason": "ho tro", "ttl_seconds": 600})
        self.assertEqual(status, 201)
        agent_client = Client(self.port)
        status, _ = agent_client.call(
            "POST", "/api/agents/lockd/lock", {"locked": False},
            bearer=data["grant"]["token"])
        self.assertEqual(status, 403)  # sensitive: operate never reaches it

    def test_lock_transport_failure_keeps_state(self):
        self._create("locke")
        headers = {"X-Admin-Reauth": ADMIN_TOKEN}
        status, _ = self.client.call("POST", "/api/agents/locke/ssh-key",
                                     {"public_key": make_key_line()},
                                     headers=headers)
        self.assertEqual(status, 200)
        self.ssh_rc[0] = 1
        try:
            status, _ = self.client.call("POST", "/api/agents/locke/lock",
                                         {"locked": False}, headers=headers)
        finally:
            self.ssh_rc[0] = 0
        self.assertEqual(status, 502)
        self.assertTrue(self._overview_agent("locke")["locked"])

    def test_page_has_lock_ui(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/")
        res = conn.getresponse()
        body = res.read().decode("utf-8")
        conn.close()
        self.assertIn("toggleLock", body)
        self.assertIn("khoá trong hub", body)

    def test_page_has_ssh_key_dialog(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/")
        res = conn.getresponse()
        body = res.read().decode("utf-8")
        conn.close()
        self.assertEqual(res.status, 200)
        self.assertIn('id="ssh-key-dialog"', body)
        self.assertIn('id="ssh-key-input"', body)
        self.assertIn("openSshKeyDialog", body)
        self.assertIn('"SSH key"', body)


if __name__ == "__main__":
    unittest.main()
