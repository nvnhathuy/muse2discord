"""Password-management tests (owner decisions 2026-11-22 10:01):
change password in the page, recovery-code reset (single-use), and the
server-side CLI reset. Plus the first-boot simplification (owner
decision 2026-11-22 12:53): a fresh deploy starts with the default
password admin123 (or $HUB_ADMIN_TOKEN when set) and the first login
is FORCED to change it before any other API call works.
Run: python3 -m unittest discover -s tests -v
"""

import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hub_admin.api import AdminApp, serve
from hub_admin.app import build_app
from hub_admin.audit import AuditLog
from hub_admin.auth import AdminAuth
from hub_admin.credentials import DEFAULT_PASSWORD, AdminCredentials
from hub_admin.delivery import DeliveryState
from hub_admin.grants import GrantStore
from hub_admin.ops_config import OpsConfig
from hub_admin.registry import AgentRegistry
from hub_admin.reset_password import reset_password
from hub_admin.secrets_store import SecretStore

OLD_PW = "old-password-1"
NEW_PW = "new-password-2"


class CredentialsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.creds = AdminCredentials(os.path.join(self.tmp, "admin_credentials.json"))
        self.creds.set_password(OLD_PW)

    def test_verify_and_wrong_password(self):
        self.assertTrue(self.creds.verify(OLD_PW))
        self.assertFalse(self.creds.verify("sai-mat-khau"))

    def test_short_password_rejected(self):
        with self.assertRaises(ValueError):
            self.creds.set_password("ngan")

    def test_version_bumps_on_set(self):
        v1 = self.creds.version()
        self.creds.set_password(NEW_PW)
        self.assertEqual(self.creds.version(), v1 + 1)

    def test_recovery_code_single_use(self):
        code = self.creds.generate_recovery_code()
        self.assertFalse(self.creds.consume_recovery_code("SAI-MA-0000"))
        self.assertTrue(self.creds.consume_recovery_code(code))
        self.assertFalse(self.creds.consume_recovery_code(code))  # second use fails

    def test_new_recovery_code_invalidates_old(self):
        old_code = self.creds.generate_recovery_code()
        self.creds.generate_recovery_code()
        self.assertFalse(self.creds.consume_recovery_code(old_code))

    def test_plaintext_never_stored(self):
        code = self.creds.generate_recovery_code()
        with open(os.path.join(self.tmp, "admin_credentials.json")) as fh:
            raw = fh.read()
        self.assertNotIn(OLD_PW, raw)
        self.assertNotIn(code, raw)


class CliResetTests(unittest.TestCase):
    def test_cli_reset_function_and_subprocess(self):
        tmp = tempfile.mkdtemp()
        creds = AdminCredentials(os.path.join(tmp, "admin_credentials.json"))
        creds.set_password(OLD_PW)
        auth = AdminAuth(creds.verify, version_provider=creds.version)
        session = auth.login(OLD_PW)
        self.assertTrue(auth.check_session(session))
        # function-level reset (what the CLI calls)
        reset_password(tmp, NEW_PW)
        self.assertTrue(creds.verify(NEW_PW))
        self.assertFalse(creds.verify(OLD_PW))
        self.assertFalse(auth.check_session(session))  # old sessions die
        # real CLI entry point, password via env for non-interactive run
        env = dict(os.environ, HUB_ADMIN_NEW_PASSWORD="third-password-3")
        result = subprocess.run(
            [sys.executable, "-m", "hub_admin.reset_password", "--state-dir", tmp],
            capture_output=True, text=True, env=env,
            cwd=os.path.join(os.path.dirname(__file__), ".."))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(AdminCredentials(os.path.join(tmp, "admin_credentials.json")).verify("third-password-3"))


class PasswordApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.creds = AdminCredentials(os.path.join(cls.tmp, "admin_credentials.json"))
        cls.creds.set_password(OLD_PW)
        secrets_store = SecretStore(os.path.join(cls.tmp, "secrets.json"))
        cls.app = AdminApp(
            registry=AgentRegistry(os.path.join(cls.tmp, "agents.json"), secret_meta=secrets_store.meta),
            secrets_store=secrets_store,
            grants=GrantStore(os.path.join(cls.tmp, "grants.json")),
            audit=AuditLog(os.path.join(cls.tmp, "audit.jsonl")),
            ops=OpsConfig(os.path.join(cls.tmp, "ops.json")),
            delivery=DeliveryState(),
            auth=AdminAuth(cls.creds.verify, version_provider=cls.creds.version),
            journal_path=os.path.join(cls.tmp, "bridge.jsonl"),
            credentials=cls.creds,
        )
        cls.server = serve(cls.app, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    # small local client (mirrors test_api.Client, password login)
    def client(self, password=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        cookie = None
        if password is not None:
            conn.request("POST", "/api/login", json.dumps({"password": password}),
                         {"Content-Type": "application/json"})
            res = conn.getresponse(); res.read()
            assert res.status == 200, res.status
            cookie = res.getheader("Set-Cookie").split(";")[0]
            conn.close()
        state = {"cookie": cookie}

        def call(method, path, body=None, headers=None, bearer=None):
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            hdrs = dict(headers or {})
            payload = None
            if body is not None:
                payload = json.dumps(body)
                hdrs["Content-Type"] = "application/json"
            if state["cookie"]:
                hdrs["Cookie"] = state["cookie"]
            if bearer:
                hdrs["Authorization"] = "Bearer " + bearer
            c.request(method, path, payload, hdrs)
            r = c.getresponse()
            raw = r.read().decode("utf-8")
            sc = r.getheader("Set-Cookie")
            if sc:
                state["cookie"] = sc.split(";")[0]
            c.close()
            return r.status, json.loads(raw) if raw else {}
        return call

    def setUp(self):
        # every test starts from the same known password
        self.creds.set_password(OLD_PW)
        self.admin = self.client(OLD_PW)

    def test_change_password_wrong_current_blocked(self):
        status, _ = self.admin("POST", "/api/change-password",
                               {"current_password": "sai", "new_password": NEW_PW})
        self.assertEqual(status, 403)
        self.assertTrue(self.creds.verify(OLD_PW))

    def test_change_password_kills_other_sessions_only(self):
        other = self.client(OLD_PW)
        status, _ = other("GET", "/api/agents")
        self.assertEqual(status, 200)
        status, _ = self.admin("POST", "/api/change-password",
                               {"current_password": OLD_PW, "new_password": NEW_PW})
        self.assertEqual(status, 200)
        status, _ = other("GET", "/api/agents")
        self.assertEqual(status, 401)  # other session invalidated
        status, _ = self.admin("GET", "/api/agents")
        self.assertEqual(status, 200)  # caller's own session survives
        fresh = self.client()
        status, _ = fresh("POST", "/api/login", {"password": OLD_PW})
        self.assertEqual(status, 401)
        ok = self.client(NEW_PW)
        status, _ = ok("GET", "/api/agents")
        self.assertEqual(status, 200)

    def test_recovery_flow(self):
        status, data = self.admin("POST", "/api/recovery-code", {})
        self.assertEqual(status, 201)
        code = data["recovery_code"]
        anon = self.client()
        status, _ = anon("POST", "/api/recover",
                         {"recovery_code": "SAI-MA-0000", "new_password": NEW_PW})
        self.assertEqual(status, 403)  # wrong code blocked
        status, data = anon("POST", "/api/recover",
                         {"recovery_code": code, "new_password": NEW_PW})
        self.assertEqual(status, 200)
        # a FRESH recovery code is issued with the reset (10:07 rule):
        # the account is never left without a backup code
        new_code = data["recovery_code"]
        self.assertTrue(new_code)
        self.assertNotEqual(new_code, code)
        status, _ = anon("POST", "/api/recover",
                         {"recovery_code": code, "new_password": "another-pass-9"})
        self.assertEqual(status, 403)  # old code cannot be reused
        status, data = anon("POST", "/api/recover",
                            {"recovery_code": new_code, "new_password": "third-password-3"})
        self.assertEqual(status, 200)  # the fresh code works
        status, _ = anon("POST", "/api/recover",
                         {"recovery_code": new_code, "new_password": "fourth-password-4"})
        self.assertEqual(status, 403)  # ... and it is single-use too
        status, _ = self.admin("GET", "/api/agents")
        self.assertEqual(status, 401)  # reset killed all old sessions
        ok = self.client("third-password-3")  # password from the second reset
        status, _ = ok("GET", "/api/agents")
        self.assertEqual(status, 200)
        status, data = ok("GET", "/api/audit")
        actions = {e["action"] for e in data["entries"]}
        self.assertIn("password_reset_recovery", actions)
        self.assertIn("create_recovery_code", actions)

    def test_full_grant_cannot_manage_password(self):
        self.admin("POST", "/api/agents", {"key": "zeta", "display_name": "Zeta"})
        status, data = self.admin("POST", "/api/grants", {
            "agent_key": "zeta", "scope": "full", "reason": "su co", "ttl_seconds": 600})
        token = data["grant"]["token"]
        agent = self.client()
        status, _ = agent("POST", "/api/change-password",
                          {"current_password": OLD_PW, "new_password": NEW_PW}, bearer=token)
        self.assertEqual(status, 403)
        status, _ = agent("POST", "/api/recovery-code", {}, bearer=token)
        self.assertEqual(status, 403)


class FirstLoginBootstrapTests(unittest.TestCase):
    """Bootstrap rules (build_app): the forced-change flag is armed
    ONLY when the credentials file is first seeded - from the env
    token when provided, else the default admin123. An existing
    credentials file is never re-seeded and never flagged."""

    def _build(self, tmp, env_token=None):
        env = dict(os.environ)
        env.pop("HUB_ADMIN_TOKEN", None)
        if env_token is not None:
            env["HUB_ADMIN_TOKEN"] = env_token
        with mock.patch.dict(os.environ, env, clear=True):
            return build_app(tmp)

    def test_flag_lifecycle_on_credentials(self):
        tmp = tempfile.mkdtemp()
        creds = AdminCredentials(os.path.join(tmp, "admin_credentials.json"))
        self.assertFalse(creds.must_change_password())  # no file yet
        creds.set_password(OLD_PW)
        self.assertFalse(creds.must_change_password())  # a plain set arms nothing
        creds.set_must_change_password()
        self.assertTrue(creds.must_change_password())
        creds.set_password(NEW_PW)  # any later set clears the flag
        self.assertFalse(creds.must_change_password())

    def test_bootstrap_default_password_is_admin123(self):
        app = self._build(tempfile.mkdtemp())
        self.assertTrue(app.credentials.verify(DEFAULT_PASSWORD))
        self.assertEqual(DEFAULT_PASSWORD, "admin123")
        self.assertTrue(app.credentials.must_change_password())

    def test_bootstrap_env_token_still_forced(self):
        app = self._build(tempfile.mkdtemp(), env_token="first-from-env-1")
        self.assertTrue(app.credentials.verify("first-from-env-1"))
        self.assertFalse(app.credentials.verify(DEFAULT_PASSWORD))
        self.assertTrue(app.credentials.must_change_password())

    def test_existing_credentials_never_flagged(self):
        tmp = tempfile.mkdtemp()
        creds = AdminCredentials(os.path.join(tmp, "admin_credentials.json"))
        creds.set_password("existing-pw-9")  # a running install's file
        app = self._build(tmp, env_token="ignored-env-token-1")
        self.assertTrue(app.credentials.verify("existing-pw-9"))
        self.assertFalse(app.credentials.verify("ignored-env-token-1"))
        self.assertFalse(app.credentials.verify(DEFAULT_PASSWORD))
        self.assertFalse(app.credentials.must_change_password())


class _HttpClient:
    """Cookie-keeping client like PasswordApiTests', reusable across
    the gate tests below (each call may carry its own cookie jar)."""

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
        status = res.status
        conn.close()
        if set_cookie:
            self.cookie = set_cookie.split(";")[0]
        return status, json.loads(raw) if raw else {}

    def login(self, password):
        return self.call("POST", "/api/login", {"password": password})


class ForcedChangeGateTests(unittest.TestCase):
    """HTTP behaviour of a fresh deploy (default bootstrap): the admin
    session is locked to /api/change-password (+ login/logout) until
    the initial password is replaced."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        env = dict(os.environ)
        env.pop("HUB_ADMIN_TOKEN", None)
        self._env_patch = mock.patch.dict(os.environ, env, clear=True)
        self._env_patch.start()
        self.app = build_app(self.tmp)
        self.server = serve(self.app, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.admin = _HttpClient(self.port)

    def tearDown(self):
        self.server.shutdown()
        self._env_patch.stop()

    def test_login_reports_flag_and_gate_blocks_api(self):
        status, data = self.admin.login(DEFAULT_PASSWORD)
        self.assertEqual(status, 200)
        self.assertTrue(data["must_change_password"])
        for method, path, body in (
            ("GET", "/api/agents", None),
            ("GET", "/api/overview", None),
            ("GET", "/api/grants", None),
            ("POST", "/api/agents", {"key": "x1", "display_name": "X1"}),
        ):
            status, data = self.admin.call(method, path, body)
            self.assertEqual(status, 403, f"{method} {path}")
            self.assertEqual(data["error"], "password_change_required")
        # logout stays reachable during the gate
        status, _ = self.admin.call("POST", "/api/logout")
        self.assertEqual(status, 200)

    def test_forced_change_rejects_default_and_short_passwords(self):
        self.admin.login(DEFAULT_PASSWORD)
        status, _ = self.admin.call("POST", "/api/change-password",
                                    {"new_password": DEFAULT_PASSWORD})
        self.assertEqual(status, 400)  # never the well-known default again
        status, _ = self.admin.call("POST", "/api/change-password",
                                    {"new_password": "ngan"})
        self.assertEqual(status, 400)  # normal length rule still applies
        status, data = self.admin.call("GET", "/api/agents")
        self.assertEqual(status, 403)  # still gated after failed attempts
        self.assertEqual(data["error"], "password_change_required")

    def test_forced_change_flow(self):
        other = _HttpClient(self.port)
        status, _ = other.login(DEFAULT_PASSWORD)
        self.assertEqual(status, 200)
        status, data = self.admin.login(DEFAULT_PASSWORD)
        self.assertEqual(status, 200)
        # no current_password needed: the session was minted by it
        status, data = self.admin.call("POST", "/api/change-password",
                                       {"new_password": NEW_PW})
        self.assertEqual(status, 200)
        self.assertFalse(data["must_change_password"])
        code = data.get("recovery_code")
        self.assertTrue(code)  # issued once, none existed before
        self.assertTrue(self.app.credentials.has_active_recovery_code())
        self.assertTrue(self.app.credentials.consume_recovery_code(code))
        # flag cleared, gate lifted for the caller's own session...
        self.assertFalse(self.app.credentials.must_change_password())
        status, _ = self.admin.call("GET", "/api/agents")
        self.assertEqual(status, 200)
        # ...while every OTHER session died with the version bump
        status, _ = other.call("GET", "/api/agents")
        self.assertEqual(status, 401)
        # the default password is dead; the new one logs in ungated
        fresh = _HttpClient(self.port)
        status, _ = fresh.login(DEFAULT_PASSWORD)
        self.assertEqual(status, 401)
        status, data = fresh.login(NEW_PW)
        self.assertEqual(status, 200)
        self.assertFalse(data["must_change_password"])

    def test_grant_identity_is_not_gated(self):
        # a grant issued out-of-band (e.g. restored state) keeps working
        # while the admin flag is on: the gate is admin-session only
        self.app.registry.create({"key": "zeta", "display_name": "Zeta"})
        _, token = self.app.grants.issue(
            agent_key="zeta", scope="full", reason="kiem chung", ttl_seconds=600)
        agent = _HttpClient(self.port)
        status, data = agent.call("GET", "/api/overview", bearer=token)
        self.assertEqual(status, 200)
        self.assertEqual(data["identity"]["kind"], "agent")

    def test_existing_install_not_gated_and_change_needs_current(self):
        # regression for the RUNNING deployment shape: credentials file
        # already exists -> no flag, no gate, classic change flow.
        tmp = tempfile.mkdtemp()
        creds = AdminCredentials(os.path.join(tmp, "admin_credentials.json"))
        creds.set_password(OLD_PW)
        app = build_app(tmp)
        server = serve(app, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = _HttpClient(server.server_address[1])
            status, data = client.login(OLD_PW)
            self.assertEqual(status, 200)
            self.assertFalse(data["must_change_password"])
            status, _ = client.call("GET", "/api/agents")
            self.assertEqual(status, 200)  # no gate
            status, _ = client.call("POST", "/api/change-password",
                                    {"new_password": NEW_PW})
            self.assertEqual(status, 403)  # current password still required
            status, data = client.call("POST", "/api/change-password",
                                       {"current_password": OLD_PW,
                                        "new_password": NEW_PW})
            self.assertEqual(status, 200)
            self.assertNotIn("recovery_code", data)  # only the forced flow issues one
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
