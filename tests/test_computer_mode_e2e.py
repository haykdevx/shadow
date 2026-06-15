"""End-to-end check for "Computer mode" in the main chat.

static/js/computerMode.js drives the /api/workspace-agent/* HTTP routes
directly (create workspace, create session, poll, resolve approvals, send
follow-up messages). This test exercises that exact request/response
sequence against the real FastAPI router and the real agent-session runner
(with a scripted model and a faked device dispatcher — no network, no real
device), proving the whole chain works end to end: workspace creation, an
auto-started session, an approval pause/resume across separate HTTP
requests, and a follow-up message that resumes the same session.
"""

import asyncio
import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.middleware.base import BaseHTTPMiddleware

import src.agent_sessions as sessions
import src.workspace_policy as policy
import src.workspace_service as workspaces
from src.workspace_policy import GrantStore

HEADERS = {"x-test-user": "alice"}


class _AuthStub(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        user = request.headers.get("x-test-user") or None
        request.state.current_user = user
        request.state.api_token = request.headers.get("x-test-api-token") == "1"
        return await call_next(request)


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(workspaces, "DATA_DIR", tmp_path)
    monkeypatch.setattr(workspaces, "WORKSPACES_PATH", tmp_path / "ws.json")
    monkeypatch.setattr(policy, "POLICY_DIR", tmp_path)
    monkeypatch.setattr(policy, "RULES_PATH", tmp_path / "rules.json")
    monkeypatch.setattr(policy, "FULL_ACCESS_PATH", tmp_path / "full.json")
    monkeypatch.setattr(policy, "AUDIT_PATH", tmp_path / "audit.log")
    monkeypatch.setattr(policy, "GRANTS", GrantStore())
    sessions._RUNNERS.clear()

    def fake_get_device(owner, device_id=None):
        if device_id == "dev-alice" and owner == "alice":
            return {"id": "dev-alice", "name": "Alice PC", "online": True, "transport": "relay"}
        from src.shadow_devices import ShadowDeviceError
        raise ShadowDeviceError("That device does not belong to your account")
    monkeypatch.setattr("src.shadow_devices.get_device", fake_get_device)

    from routes.workspace_agent_routes import setup_workspace_agent_routes
    fapp = FastAPI()
    fapp.add_middleware(_AuthStub)

    class _ConfiguredAuth:
        is_configured = True
    fapp.state.auth_manager = _ConfiguredAuth()
    fapp.include_router(setup_workspace_agent_routes())
    return fapp


async def _poll_until(client, session_id, predicate, *, attempts=100):
    session = None
    for _ in range(attempts):
        resp = await client.get(f"/api/workspace-agent/sessions/{session_id}", headers=HEADERS)
        assert resp.status_code == 200, resp.text
        session = resp.json()
        if predicate(session):
            return session
        await asyncio.sleep(0.01)
    pytest.fail(f"condition not met, last status={session['status'] if session else None}")


async def test_computer_mode_full_round_trip(app, monkeypatch, tmp_path):
    """The same HTTP sequence computerMode.js performs from main chat:
    authorize a folder, send a prompt (creates+starts a session), watch it
    pause for approval, approve it, watch it finish, then send a follow-up
    that resumes the same session."""

    queue = [
        json.dumps({"tool": "ws_write", "args": {"path": "notes.md", "text": "hello"}}),
        json.dumps({"done": True, "report": "Wrote notes.md"}),
        json.dumps({"done": True, "report": "Follow-up handled"}),
    ]

    async def fake_llm(session, messages, **kw):
        if not queue:
            return json.dumps({"done": True, "report": "script exhausted"})
        return queue.pop(0)

    monkeypatch.setattr(sessions, "_llm", fake_llm)

    dispatched = []

    def fake_dispatch_action(owner, device_id, action, args, *, confirmed, timeout=25):
        dispatched.append(action)
        return {"ok": True}

    monkeypatch.setattr("src.shadow_devices.dispatch_action", fake_dispatch_action)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. "+ Authorize folder" — create a workspace in "ask" mode.
        ws_resp = await client.post(
            "/api/workspace-agent/workspaces",
            json={"device_id": "dev-alice", "root": str(tmp_path / "proj"), "mode": "ask"},
            headers=HEADERS,
        )
        assert ws_resp.status_code == 200, ws_resp.text
        ws = ws_resp.json()

        # 2. First prompt in the chat — computerMode.send() with no mapped
        #    agent session yet, so it creates one (auto-started server-side).
        sess_resp = await client.post(
            "/api/workspace-agent/sessions",
            json={"workspace_id": ws["id"], "task": "Add a notes file",
                  "endpoint_id": "ep1", "model": "test-model"},
            headers=HEADERS,
        )
        assert sess_resp.status_code == 200, sess_resp.text
        session_id = sess_resp.json()["id"]

        # 3. computerMode.js polls /sessions/{id} until it stops running.
        session = await _poll_until(
            client, session_id,
            lambda s: s["status"] in ("waiting_approval", "completed", "failed"))
        assert session["status"] == "waiting_approval"
        approval = next(a for a in session["approvals"] if a["status"] == "pending")
        assert approval["action"] == "ws_write"

        # 4. User clicks "Allow once" on the in-chat approval card.
        approve_resp = await client.post(
            f"/api/workspace-agent/sessions/{session_id}/approvals/{approval['id']}",
            json={"decision": "allow_once"}, headers=HEADERS,
        )
        assert approve_resp.status_code == 200, approve_resp.text

        # 5. Poll again until the session completes.
        session = await _poll_until(client, session_id, lambda s: s["status"] == "completed")
        assert session["report"] == "Wrote notes.md"
        assert "notes.md" in session["files_changed"]
        assert "ws_write" in dispatched

        # 6. A follow-up prompt in the same chat reuses the agent session.
        msg_resp = await client.post(
            f"/api/workspace-agent/sessions/{session_id}/message",
            json={"text": "Now also add a TODO section"}, headers=HEADERS,
        )
        assert msg_resp.status_code == 200, msg_resp.text
        assert msg_resp.json()["queued"] is True

        session = await _poll_until(
            client, session_id,
            lambda s: s["status"] == "completed" and s["report"] == "Follow-up handled")
        assert session["report"] == "Follow-up handled"
