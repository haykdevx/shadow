"""Mission/workspace routes: only interactive cookie sessions may use them.

API tokens, the internal agent bridge, and anonymous callers must all be
rejected — an agent must never be able to approve its own gated action,
change permission modes, or arm full access.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import src.mission_policy as policy
import src.mission_workspaces as workspaces
from src.mission_policy import GrantStore


class _AuthStub(BaseHTTPMiddleware):
    """Mimics AuthMiddleware outcomes via request headers set by the test."""

    async def dispatch(self, request, call_next):
        user = request.headers.get("x-test-user") or None
        request.state.current_user = user
        request.state.api_token = request.headers.get("x-test-api-token") == "1"
        return await call_next(request)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "POLICY_DIR", tmp_path)
    monkeypatch.setattr(policy, "RULES_PATH", tmp_path / "rules.json")
    monkeypatch.setattr(policy, "FULL_ACCESS_PATH", tmp_path / "full.json")
    monkeypatch.setattr(policy, "AUDIT_PATH", tmp_path / "audit.log")
    monkeypatch.setattr(policy, "GRANTS", GrantStore())
    monkeypatch.setattr(workspaces, "DATA_DIR", tmp_path)
    monkeypatch.setattr(workspaces, "WORKSPACES_PATH", tmp_path / "ws.json")

    from routes.mission_routes import setup_mission_routes
    app = FastAPI()
    app.add_middleware(_AuthStub)

    class _ConfiguredAuth:
        is_configured = True
    app.state.auth_manager = _ConfiguredAuth()
    app.include_router(setup_mission_routes())
    return TestClient(app, raise_server_exceptions=False)


PROTECTED = [
    ("GET", "/api/missions/workspaces", None),
    ("POST", "/api/missions/workspaces", {"device_id": "dev1", "root": "/x"}),
    ("GET", "/api/missions", None),
    ("POST", "/api/missions", {"workspace_id": "w" * 8, "goal": "do something useful"}),
    ("GET", "/api/missions/policy/rules", None),
    ("POST", "/api/missions/policy/full-access", {"device_id": "dev1", "password": "x"}),
    ("POST", "/api/missions/abcdef1234/approvals/ap1", {"decision": "allow_once"}),
    ("POST", "/api/missions/abcdef1234/start", None),
    ("POST", "/api/missions/workspaces/wsid1234/action", {"action": "ws_read", "args": {}}),
]


@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_anonymous_rejected(client, method, path, body):
    response = client.request(method, path, json=body)
    assert response.status_code == 401, (path, response.status_code, response.text)


@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_api_token_rejected(client, method, path, body):
    response = client.request(method, path, json=body,
                              headers={"x-test-user": "alice", "x-test-api-token": "1"})
    assert response.status_code == 403, (path, response.status_code)


@pytest.mark.parametrize("identity", ["api", "internal-tool"])
def test_agent_bridge_identities_rejected(client, identity):
    response = client.post("/api/missions/abcdef1234/approvals/ap1",
                           json={"decision": "allow_once"},
                           headers={"x-test-user": identity})
    assert response.status_code == 403


def test_interactive_user_passes_auth_layer(client):
    response = client.get("/api/missions/workspaces", headers={"x-test-user": "alice"})
    assert response.status_code == 200
    assert response.json() == {"workspaces": []}


def test_cross_user_mission_access_404(client, tmp_path, monkeypatch):
    import src.mission_engine as engine
    monkeypatch.setattr(engine, "MISSIONS_DIR", tmp_path / "missions")
    monkeypatch.setattr("src.shadow_devices.get_device",
                        lambda owner, device_id=None: {"id": "dev1", "name": "PC", "online": True})
    ws = workspaces.create_workspace("alice", "dev1", "/home/alice/p")
    mission = engine.create_mission("alice", ws["id"], "long enough goal here",
                                    roles={"planner": {"endpoint_id": "e", "model": "m"}})
    response = client.get(f"/api/missions/{mission['id']}", headers={"x-test-user": "bob"})
    assert response.status_code == 404


def test_full_access_requires_password(client, monkeypatch):
    class FakeAuth:
        def verify_password(self, username, password):
            return password == "right"
    monkeypatch.setattr("core.auth.AuthManager", FakeAuth)
    bad = client.post("/api/missions/policy/full-access",
                      json={"device_id": "dev1", "password": "wrong"},
                      headers={"x-test-user": "alice"})
    assert bad.status_code == 403
    good = client.post("/api/missions/policy/full-access",
                       json={"device_id": "dev1", "password": "right"},
                       headers={"x-test-user": "alice"})
    assert good.status_code == 200 and good.json()["armed"]
    status = client.get("/api/missions/policy/full-access/dev1",
                        headers={"x-test-user": "alice"})
    assert status.json()["armed"] is True
    # another user does not see or share the armed state
    other = client.get("/api/missions/policy/full-access/dev1",
                       headers={"x-test-user": "bob"})
    assert other.json()["armed"] is False


def test_workspace_full_mode_requires_armed_full_access(client, monkeypatch):
    monkeypatch.setattr("src.shadow_devices.get_device",
                        lambda owner, device_id=None: {"id": "dev1", "name": "PC", "online": True})
    ws = client.post("/api/missions/workspaces",
                     json={"device_id": "dev1", "root": "/home/alice/p"},
                     headers={"x-test-user": "alice"}).json()
    response = client.put(f"/api/missions/workspaces/{ws['id']}/mode",
                          json={"mode": "full"}, headers={"x-test-user": "alice"})
    assert response.status_code == 403
    assert "Arm full access" in response.json()["detail"]


def test_persistent_rules_owner_scoped(client):
    policy.GRANTS.grant("alice", "workspace", "", "shell:pip-install",
                        workspace_id="ws1", summary="installer")
    mine = client.get("/api/missions/policy/rules", headers={"x-test-user": "alice"}).json()
    assert len(mine["rules"]) == 1
    theirs = client.get("/api/missions/policy/rules", headers={"x-test-user": "bob"}).json()
    assert theirs["rules"] == []
    rule_id = mine["rules"][0]["id"]
    stolen = client.delete(f"/api/missions/policy/rules/{rule_id}",
                           headers={"x-test-user": "bob"})
    assert stolen.status_code == 404
