import json

import pytest

from companion.home_agent import HomeAgentError, execute_action
from src import shadow_access, shadow_pc


@pytest.fixture(autouse=True)
def _clean_pending(monkeypatch, tmp_path):
    shadow_pc._PENDING.clear()
    monkeypatch.setattr(shadow_access, "ACCESS_PATH", tmp_path / "shadow-pc-access.json")
    monkeypatch.setenv("SHADOW_PC_OWNER", "admin")
    monkeypatch.setenv("SHADOW_HOME_AGENT_URL", "http://100.64.10.20:8765")
    monkeypatch.setenv("SHADOW_HOME_AGENT_TOKEN", "a" * 64)
    monkeypatch.delenv("SHADOW_HOME_AGENT_ALLOW_PUBLIC", raising=False)
    monkeypatch.delenv("SHADOW_HOME_AGENT_SOCKET", raising=False)
    yield
    shadow_pc._PENDING.clear()


def test_agent_url_accepts_tailscale_address():
    assert shadow_pc.validate_agent_url("http://100.64.10.20:8765") == "http://100.64.10.20:8765"


def test_agent_url_accepts_ipv6_loopback():
    assert shadow_pc.validate_agent_url("http://[::1]:8765") == "http://[::1]:8765"


def test_agent_url_rejects_public_address():
    with pytest.raises(shadow_pc.ShadowPcError, match="Refusing a public home-agent URL"):
        shadow_pc.validate_agent_url("https://8.8.8.8:8765")


def test_socket_transport_is_configured_without_url(monkeypatch):
    monkeypatch.delenv("SHADOW_HOME_AGENT_URL")
    monkeypatch.setenv("SHADOW_HOME_AGENT_SOCKET", "/app/data/shadow-home-agent.sock")
    assert shadow_pc.configured() is True
    assert shadow_pc._validated_agent_url() == "http://shadow-home-agent"


def test_read_action_executes_without_pending(monkeypatch):
    calls = []
    monkeypatch.setattr(shadow_pc, "_call_home_agent", lambda action, args, confirmed=False: calls.append((action, args, confirmed)) or {"ok": True})
    assert shadow_pc.request_action("status") == {"ok": True}
    assert calls == [("status", {}, False)]
    assert shadow_pc.list_pending() == []


def test_write_action_waits_for_explicit_confirmation(monkeypatch):
    calls = []
    monkeypatch.setattr(shadow_pc, "_call_home_agent", lambda action, args, confirmed=False: calls.append((action, args, confirmed)) or {"ok": True})
    proposed = shadow_pc.request_action("lock", requested_by="test")
    pending_id = proposed["pending"]["id"]
    assert proposed["status"] == "pending_confirmation"
    assert calls == []
    assert shadow_pc.list_pending()[0]["action"] == "lock"
    assert shadow_pc.confirm_action(pending_id) == {"status": "executed", "action": "lock", "result": {"ok": True}}
    assert calls == [("lock", {}, True)]
    assert shadow_pc.list_pending() == []


def test_pending_actions_are_isolated_by_principal(monkeypatch):
    monkeypatch.setattr(shadow_pc, "_call_home_agent", lambda *args, **kwargs: {"ok": True})
    pending_id = shadow_pc.request_action("lock", requested_by="web:alice", principal="alice")["pending"]["id"]

    assert shadow_pc.list_pending(principal="bob") == []
    assert [row["id"] for row in shadow_pc.list_pending(principal="alice")] == [pending_id]
    with pytest.raises(shadow_pc.ShadowPcError, match="another account"):
        shadow_pc.confirm_action(pending_id, principal="bob")
    assert shadow_pc.confirm_action(pending_id, principal="alice")["status"] == "executed"


def test_agent_pc_tool_uses_account_permissions(monkeypatch):
    monkeypatch.setattr(shadow_pc, "_call_home_agent", lambda *args, **kwargs: {"ok": True})
    denied = shadow_pc.tool_action(json.dumps({"action": "status"}), requested_by="agent:bob")
    assert denied["exit_code"] == 1
    assert "permission" in denied["error"]

    allowed = shadow_pc.tool_action(json.dumps({"action": "status"}), requested_by="agent:admin")
    assert allowed["exit_code"] == 0


def test_unconfigured_write_action_is_not_queued(monkeypatch):
    monkeypatch.delenv("SHADOW_HOME_AGENT_URL")
    monkeypatch.delenv("SHADOW_HOME_AGENT_TOKEN")
    with pytest.raises(shadow_pc.ShadowPcError, match="not configured"):
        shadow_pc.request_action("lock")
    assert shadow_pc.list_pending() == []


def test_cancel_drops_pending_action(monkeypatch):
    monkeypatch.setattr(shadow_pc, "_call_home_agent", lambda *args, **kwargs: pytest.fail("cancelled action executed"))
    pending_id = shadow_pc.request_action("type_text", {"text": "hello"})["pending"]["id"]
    assert shadow_pc.cancel_action(pending_id)["status"] == "cancelled"
    assert shadow_pc.list_pending() == []


def test_tool_action_reports_pending_approval(monkeypatch):
    monkeypatch.setattr(shadow_pc, "_call_home_agent", lambda *args, **kwargs: pytest.fail("mutation executed without approval"))
    result = shadow_pc.tool_action(json.dumps({"action": "lock"}), requested_by="agent:admin")
    assert result["exit_code"] == 0
    assert "has NOT executed" in result["output"]
    assert result["pending_confirmation"]["action"] == "lock"


def test_home_agent_rejects_unconfirmed_mutation():
    with pytest.raises(HomeAgentError, match="confirmed=true"):
        execute_action("lock", {}, confirmed=False)


def test_home_agent_status_is_read_only():
    result = execute_action("status", {}, confirmed=False)
    assert result["ok"] is True
    assert result["hostname"]


def test_home_agent_executes_confirmed_lock(monkeypatch):
    calls = []
    monkeypatch.setattr("companion.home_agent._run_first", lambda candidates: calls.append(candidates) or "")
    assert execute_action("lock", {}, confirmed=True) == {"ok": True}
    assert calls


def test_pc_control_registered_for_agent_tooling():
    from src.agent_tools import TOOL_TAGS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS

    names = {row["function"]["name"] for row in FUNCTION_TOOL_SCHEMAS}
    assert "pc_control" in TOOL_TAGS
    assert "pc_control" in NON_ADMIN_BLOCKED_TOOLS
    assert "pc_control" in names

