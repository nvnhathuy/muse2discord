"""Admin HTTP API + page server (spec 4, 5, 5b, 6, 8.2).

Framework-free (stdlib http.server) so it can sit next to the hub core
without new dependencies. One server, one address (spec 5: the page has
its OWN address; the house portal only links to it).

Identity model:
- admin principal: session cookie from POST /api/login (admin token).
  Sensitive operations (spec 6) additionally need a fresh step-up proof
  in the X-Admin-Reauth header.
- agent: Bearer token from a temporary grant (spec 5b). Scope decides
  what is allowed: read < operate < full. A "full" grant is
  admin-equivalent (owner decision 2026-11-22), sensitive ops included;
  grant ISSUE/REVOKE stays admin-only even then.
Every mutating call is audit-logged, with grant_id when an agent acts.
"""

from __future__ import annotations

import json
import os
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from .audit import AuditLog
from .auth import AdminAuth
from .delivery import DeliveryState
from .grants import Grant, GrantStore, scope_allows
from .journal import retention_report
from .ops_config import OpsConfig
from .registry import AgentRegistry, RegistryError
from .secrets_store import SecretStore

DESCRIPTIVE_FIELDS = {"display_name", "role", "description", "notes", "bot_username"}


class Identity:
    def __init__(self, kind: str, name: str, grant: Optional[Grant] = None,
                 session_id: Optional[str] = None):
        self.kind = kind  # "admin" | "agent"
        self.name = name  # "admin" | agent key holding the grant
        self.grant = grant
        self.session_id = session_id

    @property
    def grant_id(self) -> Optional[str]:
        return self.grant.id if self.grant else None


class AdminApp:
    def __init__(
        self,
        registry: AgentRegistry,
        secrets_store: SecretStore,
        grants: GrantStore,
        audit: AuditLog,
        ops: OpsConfig,
        delivery: DeliveryState,
        auth: AdminAuth,
        journal_path: str,
        status_provider: Optional[Callable[[], dict]] = None,
        static_dir: Optional[str] = None,
        credentials=None,
    ):
        self.registry = registry
        self.secrets = secrets_store
        self.grants = grants
        self.audit = audit
        self.ops = ops
        self.delivery = delivery
        self.auth = auth
        self.journal_path = journal_path
        self.status_provider = status_provider or (lambda: {
            "ready": True, "recovering": False, "journal_healthy": True,
            "online_bots": [], "expected_bots": [],
        })
        self.static_dir = static_dir or os.path.join(os.path.dirname(__file__), "static")
        # AdminCredentials or None (None in legacy token-only setups:
        # password endpoints then answer 501).
        self.credentials = credentials

    # ------------------------------------------------------------------
    # identity / authorization
    # ------------------------------------------------------------------
    def identify(self, headers) -> Optional[Identity]:
        cookie = headers.get("Cookie", "") or ""
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("admin_session="):
                session_id = part.split("=", 1)[1]
                if self.auth.check_session(session_id):
                    return Identity("admin", "admin", session_id=session_id)
        authorization = headers.get("Authorization", "") or ""
        if authorization.startswith("Bearer "):
            grant = self.grants.authenticate(authorization[len("Bearer "):].strip())
            if grant:
                return Identity("agent", grant.agent_key, grant)
        return None

    def authorize(self, identity: Identity, op: str) -> None:
        if identity.kind == "admin":
            return
        if identity.grant is None or not scope_allows(identity.grant.scope, op):
            raise PermissionError(f"operation {op!r} not allowed for this identity")

    def require_step_up(self, identity: Identity, headers) -> None:
        if identity.kind == "agent":
            # A "full" grant IS the admin's step-up decision, taken when
            # the grant was issued (owner decision 2026-11-22 09:50):
            # the holder acts for the admin, sensitive ops included.
            # authorize() has already rejected non-full grants.
            if identity.grant is not None and identity.grant.scope == "full":
                return
            raise PermissionError("sensitive operations need admin or a full temporary grant")
        if not self.auth.verify_step_up(headers.get("X-Admin-Reauth", "")):
            raise PermissionError("sensitive operation requires fresh re-authentication")

    # ------------------------------------------------------------------
    # endpoint handlers (each returns (status, payload))
    # ------------------------------------------------------------------
    def overview(self, identity: Identity):
        self.authorize(identity, "view")
        status = self.status_provider()
        agents = []
        for profile in self.registry.list_agents():
            row = profile.public_dict(self.secrets.meta(profile.key))
            row.update(self.delivery.probe(profile.key))
            row["online"] = profile.key in (status.get("online_bots") or [])
            agents.append(row)
        return 200, {
            "hub": status,
            "agents": agents,
            "identity": {
                "kind": identity.kind,
                "name": identity.name,
                "scope": identity.grant.scope if identity.grant else None,
            },
        }

    def list_agents(self, identity: Identity):
        self.authorize(identity, "view")
        return 200, {"agents": self.registry.public_list()}

    def create_agent(self, identity: Identity, body: dict):
        if identity.kind != "admin":
            raise PermissionError("only the admin principal can create agents")
        profile = self.registry.create(body)
        token = body.get("bot_token")
        if token:
            self.secrets.set(profile.key, token)
        self.audit.record(identity.name, "create_agent", profile.key,
                          {"display_name": profile.display_name}, grant_id=identity.grant_id)
        return 201, {"agent": self.registry.public_get(profile.key)}

    def update_agent(self, identity: Identity, key: str, body: dict):
        if identity.kind == "agent":
            unknown = set(body) - DESCRIPTIVE_FIELDS
            if unknown:
                raise PermissionError(f"grant scope cannot edit fields: {sorted(unknown)}")
            self.authorize(identity, "edit_agent_descriptive")
        profile = self.registry.update(key, body)
        self.audit.record(identity.name, "update_agent", key,
                          {"fields": sorted(body)}, grant_id=identity.grant_id)
        return 200, {"agent": self.registry.public_get(profile.key)}

    def set_enabled(self, identity: Identity, key: str, enabled: bool, body: dict):
        self.authorize(identity, "resume_agent" if enabled else "pause_agent")
        reason = body.get("reason", "")
        if not enabled and not reason:
            raise ValueError("pausing requires a reason (spec 4.4)")
        profile = self.registry.set_enabled(key, enabled, by=identity.name, reason=reason)
        self.audit.record(identity.name, "resume_agent" if enabled else "pause_agent",
                          key, {"reason": reason, "drain": bool(body.get("drain", True))},
                          grant_id=identity.grant_id)
        return 200, {"agent": self.registry.public_get(profile.key)}

    def delete_agent(self, identity: Identity, key: str, body: dict, headers):
        self.authorize(identity, "delete_agent")  # grants: always denied
        self.require_step_up(identity, headers)
        profile = self.registry.get(key)
        confirm = body.get("confirm_name", "")
        if confirm not in (profile.key, profile.display_name):
            raise ValueError("type the agent key or display name to confirm hard delete")
        self.registry.hard_delete(key, self.delivery.probe)
        self.secrets.delete(key)
        self.audit.record(identity.name, "delete_agent", key,
                          {"hard": True}, grant_id=identity.grant_id)
        return 200, {"deleted": key}

    def set_token(self, identity: Identity, key: str, body: dict, headers):
        self.authorize(identity, "set_bot_token")
        self.require_step_up(identity, headers)
        self.registry.get(key)  # must exist
        self.secrets.set(key, body.get("bot_token", ""))
        self.audit.record(identity.name, "set_bot_token", key,
                          {"length": len(body.get("bot_token", ""))},
                          grant_id=identity.grant_id)
        return 200, {"agent": self.registry.public_get(key)}

    def queue(self, identity: Identity, key: str):
        self.authorize(identity, "view")
        self.registry.get(key)
        return 200, {
            "agent": key,
            "cursor": self.delivery.cursor_of(key),
            "items": self.delivery.queue_for(key),
        }

    def release_lease(self, identity: Identity, key: str, body: dict):
        self.authorize(identity, "release_lease")
        record = self.delivery.force_release(key, body["message_id"])
        self.audit.record(identity.name, "force_release_lease", key,
                          {"message_id": body["message_id"]}, grant_id=identity.grant_id)
        return 200, {"released": record}

    def ack_on_behalf(self, identity: Identity, key: str, body: dict, headers):
        self.authorize(identity, "ack_on_behalf")
        self.require_step_up(identity, headers)
        done = self.delivery.ack_on_behalf(key, body["message_id"])
        self.audit.record(identity.name, "ack_on_behalf", key,
                          {"message_id": body["message_id"], "acked": done},
                          grant_id=identity.grant_id)
        return 200, {"acked": done}

    def reset_cursor(self, identity: Identity, key: str, body: dict, headers):
        self.authorize(identity, "reset_cursor")
        self.require_step_up(identity, headers)
        previous = self.delivery.reset_cursor(key, body.get("to_message_id", "0"))
        self.audit.record(identity.name, "reset_cursor", key,
                          {"from": previous, "to": body.get("to_message_id", "0")},
                          grant_id=identity.grant_id)
        return 200, {"cursor": body.get("to_message_id", "0"), "previous": previous}

    def get_ops(self, identity: Identity):
        self.authorize(identity, "view")
        return 200, {"ops_config": self.ops.get_all()}

    def update_ops(self, identity: Identity, body: dict, headers):
        self.authorize(identity, "edit_ops_config")
        self.require_step_up(identity, headers)
        diff = self.ops.update(body.get("changes", {}))
        self.audit.record(identity.name, "edit_ops_config", "",
                          {"changes": {k: [o, n] for k, (o, n) in diff.items()}},
                          grant_id=identity.grant_id)
        return 200, {"ops_config": self.ops.get_all()}

    def get_audit(self, identity: Identity, query: dict):
        self.authorize(identity, "view")
        entries = self.audit.query(
            agent=query.get("agent", [None])[0],
            action=query.get("action", [None])[0],
            limit=int(query.get("limit", ["200"])[0]),
        )
        return 200, {"entries": entries}

    def get_journal(self, identity: Identity):
        self.authorize(identity, "view")
        cfg = self.ops.get_all()
        return 200, {"journal": retention_report(
            self.journal_path, int(cfg["journal_online_days"]), int(cfg["journal_rotate_mb"]))}

    def list_grants(self, identity: Identity):
        if identity.kind != "admin":
            raise PermissionError("grants are managed by the admin principal only")
        return 200, {"grants": [g.public_dict() for g in self.grants.list_grants()]}

    def issue_grant(self, identity: Identity, body: dict):
        if identity.kind != "admin":
            raise PermissionError("only the admin principal can issue grants")
        self.registry.get(body.get("agent_key", ""))  # agent must exist
        grant, token = self.grants.issue(
            agent_key=body["agent_key"],
            scope=body.get("scope", "read"),
            reason=body.get("reason", ""),
            ttl_seconds=body.get("ttl_seconds"),
        )
        self.audit.record(identity.name, "issue_grant", grant.agent_key,
                          {"grant_id": grant.id, "scope": grant.scope,
                           "expires_at": grant.expires_at, "reason": grant.reason})
        payload = grant.public_dict()
        payload["token"] = token  # shown exactly once
        return 201, {"grant": payload}

    def revoke_grant(self, identity: Identity, grant_id: str):
        if identity.kind != "admin":
            raise PermissionError("only the admin principal can revoke grants")
        grant = self.grants.revoke(grant_id)
        self.audit.record(identity.name, "revoke_grant", grant.agent_key,
                          {"grant_id": grant.id})
        return 200, {"grant": grant.public_dict()}

    # ------------------------------------------------------------------
    # admin password: change / recovery code / forgot-password reset
    # Credential management belongs to the human admin's own session -
    # never to a grant, not even a full one (same line as grant issuing).
    # ------------------------------------------------------------------
    def _require_credentials(self):
        if self.credentials is None:
            raise RuntimeError("password management is not configured on this deployment")
        return self.credentials

    def _require_admin_session(self, identity: Identity):
        if identity.kind != "admin" or identity.session_id is None:
            raise PermissionError("this action needs the admin's own session")

    def change_password(self, identity: Identity, body: dict):
        self._require_admin_session(identity)
        credentials = self._require_credentials()
        if not credentials.verify(body.get("current_password", "")):
            raise PermissionError("current password is wrong")
        credentials.set_password(body.get("new_password", ""))
        # version bump kills every other session; keep the caller's own
        self.auth.restamp_session(identity.session_id)
        self.audit.record("admin", "change_password", "",
                          {"sessions_invalidated": "all-others"})
        return 200, {"ok": True}

    def create_recovery_code(self, identity: Identity):
        self._require_admin_session(identity)
        credentials = self._require_credentials()
        code = credentials.generate_recovery_code()
        self.audit.record("admin", "create_recovery_code", "", {})
        return 201, {"recovery_code": code}  # shown exactly once

    def recover_password(self, body: dict):
        """Forgot-password path from the login screen: recovery code +
        new password. Public endpoint - the code IS the credential.
        On success a FRESH recovery code is issued and returned once,
        so the account is never left without a backup code (owner
        decision 2026-11-22 10:07); the used code is dead for good."""
        credentials = self._require_credentials()
        if not credentials.consume_recovery_code(body.get("recovery_code", "")):
            raise PermissionError("recovery code is wrong or already used")
        credentials.set_password(body.get("new_password", ""))
        self.auth.invalidate_all_sessions()
        self.audit.record("admin", "password_reset_recovery", "",
                          {"sessions_invalidated": "all"})
        new_code = credentials.generate_recovery_code()
        self.audit.record("admin", "create_recovery_code", "",
                          {"via": "recovery_reset"})
        return 200, {"ok": True, "recovery_code": new_code}  # shown exactly once


# ----------------------------------------------------------------------
# HTTP plumbing
# ----------------------------------------------------------------------

def make_handler(app: AdminApp):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # keep stdout clean
            pass

        # -- helpers ---------------------------------------------------
        def _send_json(self, status: int, payload: dict, extra_headers=None):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _send_page(self):
            page = os.path.join(app.static_dir, "admin.html")
            try:
                with open(page, "rb") as fh:
                    body = fh.read()
            except OSError:
                return self._send_json(500, {"error": "admin page missing"})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}

        def _dispatch(self, method: str):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)
            try:
                if path in ("/", "/admin") and method == "GET":
                    return self._send_page()
                if path == "/api/login" and method == "POST":
                    body = self._body()
                    session = app.auth.login(body.get("password", body.get("token", "")))
                    if not session:
                        return self._send_json(401, {"error": "invalid admin password"})
                    return self._send_json(200, {"ok": True}, {
                        "Set-Cookie": f"admin_session={session}; HttpOnly; Path=/; SameSite=Strict"})
                if path == "/api/logout" and method == "POST":
                    identity = app.identify(self.headers)
                    if identity is not None and identity.session_id:
                        app.auth.logout(identity.session_id)
                    return self._send_json(200, {"ok": True}, {
                        "Set-Cookie": "admin_session=; HttpOnly; Path=/; SameSite=Strict; Max-Age=0"})
                if path == "/api/recover" and method == "POST":
                    try:
                        status, payload = app.recover_password(self._body())
                    except PermissionError as exc:
                        return self._send_json(403, {"error": str(exc)})
                    except (ValueError, RuntimeError) as exc:
                        return self._send_json(400, {"error": str(exc)})
                    return self._send_json(status, payload)
                identity = app.identify(self.headers)
                if identity is None:
                    return self._send_json(401, {"error": "authentication required"})
                status, payload = self._route(app, method, path, query, identity, self._body(), self.headers)
                return self._send_json(status, payload)
            except PermissionError as exc:
                return self._send_json(403, {"error": str(exc)})
            except (RegistryError, ValueError, KeyError, RuntimeError) as exc:
                return self._send_json(400, {"error": str(exc)})

        @staticmethod
        def _route(app, method, path, query, identity, body, headers):
            parts = [p for p in path.split("/") if p]
            if parts == ["api", "overview"] and method == "GET":
                return app.overview(identity)
            if parts == ["api", "agents"] and method == "GET":
                return app.list_agents(identity)
            if parts == ["api", "agents"] and method == "POST":
                return app.create_agent(identity, body)
            if len(parts) == 3 and parts[:2] == ["api", "agents"]:
                key = parts[2]
                if method == "GET":
                    app.authorize(identity, "view")
                    return 200, {"agent": app.registry.public_get(key)}
                if method == "PATCH":
                    return app.update_agent(identity, key, body)
                if method == "DELETE":
                    return app.delete_agent(identity, key, body, headers)
            if len(parts) == 4 and parts[:2] == ["api", "agents"]:
                key, action = parts[2], parts[3]
                if action == "pause" and method == "POST":
                    return app.set_enabled(identity, key, False, body)
                if action == "resume" and method == "POST":
                    return app.set_enabled(identity, key, True, body)
                if action == "token" and method == "POST":
                    return app.set_token(identity, key, body, headers)
                if action == "queue" and method == "GET":
                    return app.queue(identity, key)
            if len(parts) == 5 and parts[:2] == ["api", "agents"] and parts[3] == "queue":
                key, action = parts[2], parts[4]
                if action == "release" and method == "POST":
                    return app.release_lease(identity, key, body)
                if action == "ack-on-behalf" and method == "POST":
                    return app.ack_on_behalf(identity, key, body, headers)
                if action == "reset-cursor" and method == "POST":
                    return app.reset_cursor(identity, key, body, headers)
            if parts == ["api", "ops-config"] and method == "GET":
                return app.get_ops(identity)
            if parts == ["api", "ops-config"] and method == "PATCH":
                return app.update_ops(identity, body, headers)
            if parts == ["api", "audit"] and method == "GET":
                return app.get_audit(identity, query)
            if parts == ["api", "journal"] and method == "GET":
                return app.get_journal(identity)
            if parts == ["api", "grants"] and method == "GET":
                return app.list_grants(identity)
            if parts == ["api", "grants"] and method == "POST":
                return app.issue_grant(identity, body)
            if len(parts) == 3 and parts[:2] == ["api", "grants"] and method == "DELETE":
                return app.revoke_grant(identity, parts[2])
            if parts == ["api", "change-password"] and method == "POST":
                return app.change_password(identity, body)
            if parts == ["api", "recovery-code"] and method == "POST":
                return app.create_recovery_code(identity)
            return 404, {"error": f"no route for {method} {path}"}

        do_GET = lambda self: self._dispatch("GET")
        do_POST = lambda self: self._dispatch("POST")
        do_PATCH = lambda self: self._dispatch("PATCH")
        do_DELETE = lambda self: self._dispatch("DELETE")

    return Handler


def serve(app: AdminApp, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    return server
