"""Computer-access RBAC privilege gate and the audit log endpoint.

``can_use_computer`` controls every workspace-agent write/dispatch route —
strictly more powerful than ``can_use_bash``, so it defaults off for new
non-admin accounts and admins always have it via ``ADMIN_PRIVILEGES``.
"""

import ast
import json
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import src.workspace_policy as policy
import src.workspace_service as workspaces
from core.auth import ADMIN_PRIVILEGES, DEFAULT_PRIVILEGES
from src.workspace_policy import GrantStore, read_audit_log


# ── RBAC defaults ────────────────────────────────────────────────────────


def test_computer_access_defaults_off_for_new_accounts():
    assert DEFAULT_PRIVILEGES["can_use_computer"] is False


def test_admins_always_have_computer_access():
    assert ADMIN_PRIVILEGES["can_use_computer"] is True


# ── route source: every mutating/dispatch route is gated ───────────────


GATED_FUNCTIONS = {
    "workspaces_list",
    "workspaces_create",
    "workspaces_mode",
    "workspaces_action",
    "policy_full_access",
    "sessions_list",
    "sessions_create",
    "session_detail",
    "session_message",
    "session_resume",
    "session_retry",
    "session_rollback",
    "session_approval",
}


def _route_function_sources():
    source = Path("routes/workspace_agent_routes.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    return {
        node.name: ast.get_source_segment(source, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_workspace_agent_routes_require_computer_access():
    functions = _route_function_sources()
    for name in GATED_FUNCTIONS:
        assert name in functions, name
        assert "_require_computer_access(request)" in functions[name], name


# ── read_audit_log ───────────────────────────────────────────────────────


@pytest.fixture()
def audit_path(tmp_path, monkeypatch):
    path = tmp_path / "audit.log"
    monkeypatch.setattr(policy, "AUDIT_PATH", path)
    return path


def _write_records(path, records):
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def test_read_audit_log_most_recent_first(audit_path):
    _write_records(audit_path, [
        {"ts": 1, "owner": "alice", "event": "workspace_authorized", "detail": {}},
        {"ts": 2, "owner": "alice", "event": "action_executed", "detail": {}},
        {"ts": 3, "owner": "bob", "event": "action_executed", "detail": {}},
    ])
    records = read_audit_log()
    assert [r["ts"] for r in records] == [3, 2, 1]


def test_read_audit_log_owner_scoped(audit_path):
    _write_records(audit_path, [
        {"ts": 1, "owner": "alice", "event": "workspace_authorized", "detail": {}},
        {"ts": 2, "owner": "bob", "event": "workspace_authorized", "detail": {}},
    ])
    assert [r["owner"] for r in read_audit_log(owner="alice")] == ["alice"]
    assert [r["owner"] for r in read_audit_log(owner="bob")] == ["bob"]
    assert len(read_audit_log(owner=None)) == 2


def test_read_audit_log_event_and_pagination(audit_path):
    _write_records(audit_path, [
        {"ts": 1, "owner": "alice", "event": "workspace_authorized", "detail": {}},
        {"ts": 2, "owner": "alice", "event": "action_executed", "detail": {}},
        {"ts": 3, "owner": "alice", "event": "action_executed", "detail": {}},
    ])
    only_actions = read_audit_log(event="action_executed")
    assert [r["ts"] for r in only_actions] == [3, 2]

    page1 = read_audit_log(limit=1)
    assert [r["ts"] for r in page1] == [3]
    page2 = read_audit_log(limit=1, before=page1[0]["ts"])
    assert [r["ts"] for r in page2] == [2]


def test_read_audit_log_missing_file(audit_path):
    assert read_audit_log() == []


# ── /audit route ─────────────────────────────────────────────────────────


class _AuthStub(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request.state.current_user = request.headers.get("x-test-user") or None
        request.state.api_token = False
        return await call_next(request)


class _FakeAuthManager:
    def __init__(self, admins):
        self._admins = set(admins)

    def is_admin(self, username):
        return username in self._admins

    def get_privileges(self, username):
        privs = dict(DEFAULT_PRIVILEGES)
        privs["can_use_computer"] = True
        return privs


@pytest.fixture()
def audit_client(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "POLICY_DIR", tmp_path)
    monkeypatch.setattr(policy, "RULES_PATH", tmp_path / "rules.json")
    monkeypatch.setattr(policy, "FULL_ACCESS_PATH", tmp_path / "full.json")
    monkeypatch.setattr(policy, "AUDIT_PATH", tmp_path / "audit.log")
    monkeypatch.setattr(policy, "GRANTS", GrantStore())
    monkeypatch.setattr(workspaces, "DATA_DIR", tmp_path)
    monkeypatch.setattr(workspaces, "WORKSPACES_PATH", tmp_path / "ws.json")

    _write_records(tmp_path / "audit.log", [
        {"ts": 1, "owner": "alice", "event": "workspace_authorized", "detail": {"root": "/a"}},
        {"ts": 2, "owner": "bob", "event": "workspace_authorized", "detail": {"root": "/b"}},
    ])

    from routes.workspace_agent_routes import setup_workspace_agent_routes
    app = FastAPI()
    app.add_middleware(_AuthStub)
    app.state.auth_manager = _FakeAuthManager(admins={"admin"})
    app.include_router(setup_workspace_agent_routes())
    return TestClient(app, raise_server_exceptions=False)


def test_audit_route_scopes_non_admin_to_own_records(audit_client):
    response = audit_client.get("/api/workspace-agent/audit", headers={"x-test-user": "alice"})
    assert response.status_code == 200
    data = response.json()
    assert data["is_admin"] is False
    assert [e["owner"] for e in data["events"]] == ["alice"]


def test_audit_route_admin_sees_everything(audit_client):
    response = audit_client.get("/api/workspace-agent/audit", headers={"x-test-user": "admin"})
    assert response.status_code == 200
    data = response.json()
    assert data["is_admin"] is True
    assert sorted(e["owner"] for e in data["events"]) == ["alice", "bob"]


# ── privilege enforcement on a mutating route ───────────────────────────


class _NoComputerAuthManager:
    """Mirrors a real non-admin account with can_use_computer left at default (False)."""

    def is_admin(self, username):
        return False

    def get_privileges(self, username):
        return dict(DEFAULT_PRIVILEGES)


def test_workspace_create_denied_without_computer_privilege(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "POLICY_DIR", tmp_path)
    monkeypatch.setattr(policy, "RULES_PATH", tmp_path / "rules.json")
    monkeypatch.setattr(policy, "FULL_ACCESS_PATH", tmp_path / "full.json")
    monkeypatch.setattr(policy, "AUDIT_PATH", tmp_path / "audit.log")
    monkeypatch.setattr(policy, "GRANTS", GrantStore())
    monkeypatch.setattr(workspaces, "DATA_DIR", tmp_path)
    monkeypatch.setattr(workspaces, "WORKSPACES_PATH", tmp_path / "ws.json")

    from routes.workspace_agent_routes import setup_workspace_agent_routes
    app = FastAPI()
    app.add_middleware(_AuthStub)
    app.state.auth_manager = _NoComputerAuthManager()
    app.include_router(setup_workspace_agent_routes())
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/workspace-agent/workspaces",
        json={"device_id": "dev1", "root": "/home/alice/p"},
        headers={"x-test-user": "alice"},
    )
    assert response.status_code == 403
    assert "computer" in response.json()["detail"].lower()
