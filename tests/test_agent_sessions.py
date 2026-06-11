"""Direct Agent Sessions: tool loop, checkpoints, follow-ups, provider
failure handling, fallback, retry, rollback, recovery — with a fake model
and a fake device dispatcher (no network, no real device)."""

import asyncio
import json

import pytest

import src.agent_sessions as sessions
import src.mission_policy as policy
import src.mission_workspaces as workspaces
from src.agent_sessions import AgentSessionError, ProviderError
from src.mission_policy import GrantStore


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
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
        raise __import__("src.shadow_devices", fromlist=["x"]).ShadowDeviceError(
            "That device does not belong to your account")
    monkeypatch.setattr("src.shadow_devices.get_device", fake_get_device)
    yield


@pytest.fixture()
def alice_ws():
    return workspaces.create_workspace("alice", "dev-alice", "/home/alice/proj",
                                       "proj", mode="unattended")


MODEL = {"endpoint_id": "ep1", "model": "test-model"}


def make_session(alice_ws, task="Fix the bug in utils.py", **kw):
    return sessions.create_session("alice", alice_ws["id"], task, model=MODEL, **kw)


def fake_dispatcher(script=None):
    calls = []

    def dispatch_action(owner, device_id, action, args, *, confirmed, timeout=25):
        calls.append({"owner": owner, "action": action, "args": args, "confirmed": confirmed})
        if script and action in script:
            handler = script[action]
            return handler(args) if callable(handler) else handler
        return {"ok": True}
    dispatch_action.calls = calls
    return dispatch_action


def scripted_llm(replies):
    """Fake _llm popping scripted replies; raises when the script runs dry."""
    queue = list(replies)

    async def fake_llm(session, messages, **kw):
        if not queue:
            return json.dumps({"done": True, "report": "script exhausted"})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    return fake_llm


def run(monkeypatch, session, llm, dispatcher=None):
    monkeypatch.setattr(sessions, "_llm", llm)
    monkeypatch.setattr("src.shadow_devices.dispatch_action", dispatcher or fake_dispatcher())
    asyncio.run(sessions._runner(session["id"], "alice"))
    return sessions.load_session("alice", session["id"])


# ── the loop ─────────────────────────────────────────────────────────────

def test_session_edits_then_completes(monkeypatch, alice_ws):
    dispatcher = fake_dispatcher()
    record = run(monkeypatch, make_session(alice_ws), scripted_llm([
        json.dumps({"tool": "ws_read", "args": {"path": "utils.py"}}),
        json.dumps({"tool": "ws_patch", "args": {"edits": [{"path": "utils.py", "old": "a", "new": "b"}]}}),
        json.dumps({"tool": "ws_run", "args": {"command": "pytest -q"}}),
        json.dumps({"done": True, "report": "Patched utils.py; pytest passed."}),
    ]), dispatcher)
    assert record["status"] == "completed"
    assert record["report"].startswith("Patched")
    assert "utils.py" in record["files_read"]
    assert "utils.py" in record["files_changed"]
    assert record["commands"][0]["command"] == "pytest -q"
    actions = [c["action"] for c in dispatcher.calls]
    # Checkpoint opened automatically before the first mutation.
    assert actions.index("ws_checkpoint") < actions.index("ws_patch")
    assert record["checkpoint"]["id"] == f"s-{record['id']}"


def test_terminal_output_recorded(monkeypatch, alice_ws):
    dispatcher = fake_dispatcher({"ws_run": {"ok": True, "returncode": 0,
                                             "stdout": "2 passed", "stderr": "", "seconds": 1.2}})
    record = run(monkeypatch, make_session(alice_ws), scripted_llm([
        json.dumps({"tool": "ws_run", "args": {"command": "pytest -q"}}),
        json.dumps({"done": True, "report": "ok"}),
    ]), dispatcher)
    assert record["terminal"][0]["stdout"] == "2 passed"
    assert record["terminal"][0]["returncode"] == 0


def test_malformed_output_repaired_once(monkeypatch, alice_ws):
    record = run(monkeypatch, make_session(alice_ws), scripted_llm([
        "let me think about this...",
        json.dumps({"done": True, "report": "fixed after repair"}),
    ]))
    assert record["status"] == "completed"
    assert any("not one valid JSON object" in m["content"]
               for m in record["transcript"] if m["role"] == "user")


def test_unknown_tool_is_an_error_not_an_action(monkeypatch, alice_ws):
    dispatcher = fake_dispatcher()
    record = run(monkeypatch, make_session(alice_ws), scripted_llm([
        json.dumps({"tool": "rm_everything", "args": {}}),
        json.dumps({"done": True, "report": "ok"}),
    ]), dispatcher)
    assert record["status"] == "completed"
    assert dispatcher.calls == []  # nothing was executed
    assert any("Unknown tool: rm_everything" in m["content"] for m in record["transcript"])


def test_policy_denial_streams_back_and_session_adapts(monkeypatch, alice_ws):
    record = run(monkeypatch, make_session(alice_ws), scripted_llm([
        json.dumps({"tool": "ws_run", "args": {"command": "sudo make install"}}),
        json.dumps({"done": True, "report": "adapted without sudo"}),
    ]))
    assert record["status"] == "completed"
    assert any("DENIED BY POLICY" in m["content"] for m in record["transcript"])


def test_model_fail_verdict(monkeypatch, alice_ws):
    record = run(monkeypatch, make_session(alice_ws), scripted_llm([
        json.dumps({"fail": "the repository does not contain utils.py"}),
    ]))
    assert record["status"] == "failed"
    assert record["retryable"] is True


# ── provider failures ────────────────────────────────────────────────────

def test_provider_error_marks_failed_retryable(monkeypatch, alice_ws):
    record = run(monkeypatch, make_session(alice_ws), scripted_llm([
        ProviderError("429 rate limited by provider"),
    ]))
    assert record["status"] == "failed"
    assert record["retryable"] is True
    assert "429" in record["provider_error"]
    assert "model provider failed" in record["error"]


def test_auto_fallback_rotates_model(monkeypatch, alice_ws):
    session = make_session(
        alice_ws, auto_fallback=True,
        fallbacks=[{"endpoint_id": "ep2", "model": "backup-model"}])
    record = run(monkeypatch, session, scripted_llm([
        ProviderError("503 upstream outage"),
        json.dumps({"done": True, "report": "finished on backup"}),
    ]))
    assert record["status"] == "completed"
    assert record["model"]["model"] == "backup-model"
    assert any(e["kind"] == "model_fallback" for e in record["events"])


def test_no_fallback_without_opt_in(monkeypatch, alice_ws):
    session = make_session(
        alice_ws, auto_fallback=False,
        fallbacks=[{"endpoint_id": "ep2", "model": "backup-model"}])
    record = run(monkeypatch, session, scripted_llm([ProviderError("503")]))
    assert record["status"] == "failed"
    assert record["model"]["model"] == "test-model"  # unchanged


def test_retry_with_another_model(monkeypatch, alice_ws):
    record = run(monkeypatch, make_session(alice_ws), scripted_llm([ProviderError("down")]))
    assert record["status"] == "failed"

    async def drive():
        sessions.retry_session("alice", record["id"], endpoint_id="ep3", model="third-model")
        await sessions._RUNNERS[record["id"]]["task"]
    monkeypatch.setattr(sessions, "_llm",
                        scripted_llm([json.dumps({"done": True, "report": "third time lucky"})]))
    monkeypatch.setattr("src.shadow_devices.dispatch_action", fake_dispatcher())
    asyncio.run(drive())
    fresh = sessions.load_session("alice", record["id"])
    assert fresh["status"] == "completed"
    assert fresh["model"]["model"] == "third-model"


# ── conversation continuity ──────────────────────────────────────────────

def test_followup_message_continues_in_context(monkeypatch, alice_ws):
    record = run(monkeypatch, make_session(alice_ws), scripted_llm([
        json.dumps({"done": True, "report": "first part done"}),
    ]))
    assert record["status"] == "completed"
    sessions.send_message("alice", record["id"], "Now also update the README")

    seen_messages = []

    async def checking_llm(session, messages, **kw):
        seen_messages.extend(messages)
        return json.dumps({"done": True, "report": "README updated"})

    async def drive():
        sessions.start_session("alice", record["id"])
        await sessions._RUNNERS[record["id"]]["task"]
    monkeypatch.setattr(sessions, "_llm", checking_llm)
    monkeypatch.setattr("src.shadow_devices.dispatch_action", fake_dispatcher())
    asyncio.run(drive())
    fresh = sessions.load_session("alice", record["id"])
    assert fresh["status"] == "completed"
    contents = [m["content"] for m in seen_messages]
    assert any("Fix the bug in utils.py" in c for c in contents)   # original task
    assert any("Now also update the README" in c for c in contents)  # follow-up
    assert fresh["inbox"] == []


def test_completed_session_requires_followup_to_restart(alice_ws, monkeypatch):
    record = run(monkeypatch, make_session(alice_ws), scripted_llm([
        json.dumps({"done": True, "report": "done"}),
    ]))

    async def drive():
        sessions.start_session("alice", record["id"])
    with pytest.raises(AgentSessionError, match="follow-up"):
        asyncio.run(drive())


# ── stop / rollback / recovery / isolation ───────────────────────────────

def test_stop_session_is_resumable(monkeypatch, alice_ws):
    session = make_session(alice_ws)

    async def drive():
        started = asyncio.Event()

        async def slow_llm(s, messages, **kw):
            started.set()
            await asyncio.sleep(30)
            return "{}"
        monkeypatch.setattr(sessions, "_llm", slow_llm)
        sessions.start_session("alice", session["id"])
        await asyncio.wait_for(started.wait(), 5)
        sessions.stop_session("alice", session["id"])
        try:
            await sessions._RUNNERS.get(session["id"], {}).get("task", asyncio.sleep(0))
        except (asyncio.CancelledError, TypeError):
            pass
        await asyncio.sleep(0.05)
    monkeypatch.setattr("src.shadow_devices.dispatch_action", fake_dispatcher())
    asyncio.run(drive())
    fresh = sessions.load_session("alice", session["id"])
    assert fresh["status"] == "stopped"
    assert fresh["retryable"] is True


def test_rollback_restores_checkpoint(monkeypatch, alice_ws):
    dispatcher = fake_dispatcher({"ws_restore": {"ok": True, "restored": ["utils.py"],
                                                 "removed_created": []}})
    record = run(monkeypatch, make_session(alice_ws), scripted_llm([
        json.dumps({"tool": "ws_write", "args": {"path": "utils.py", "text": "x"}}),
        json.dumps({"done": True, "report": "ok"}),
    ]), dispatcher)
    result = asyncio.run(sessions.rollback_session("alice", record["id"]))
    assert result["restored"] == ["utils.py"]
    fresh = sessions.load_session("alice", record["id"])
    assert fresh["status"] == "rolled_back"
    restore_call = next(c for c in dispatcher.calls if c["action"] == "ws_restore")
    assert restore_call["args"]["checkpoint_id"] == f"s-{record['id']}"


def test_rollback_without_changes_refused(monkeypatch, alice_ws):
    record = run(monkeypatch, make_session(alice_ws), scripted_llm([
        json.dumps({"done": True, "report": "read-only run"}),
    ]))
    with pytest.raises(AgentSessionError, match="no checkpointed changes"):
        asyncio.run(sessions.rollback_session("alice", record["id"]))


def test_recover_sessions_unsticks_running(monkeypatch, alice_ws):
    session = make_session(alice_ws)
    session["status"] = "running"
    sessions._save(session)
    assert sessions.recover_sessions() == 1
    fresh = sessions.load_session("alice", session["id"])
    assert fresh["status"] == "stopped"
    assert fresh["retryable"] is True
    assert any(e["kind"] == "recovered" for e in fresh["events"])


def test_cross_user_session_access_rejected(alice_ws):
    session = make_session(alice_ws)
    with pytest.raises(AgentSessionError, match="does not belong"):
        sessions.load_session("bob", session["id"])
    with pytest.raises(AgentSessionError, match="does not belong"):
        sessions.send_message("bob", session["id"], "hi")


def test_session_needs_real_task_and_model(alice_ws):
    with pytest.raises(AgentSessionError, match="real task"):
        sessions.create_session("alice", alice_ws["id"], "x", model=MODEL)
    with pytest.raises(AgentSessionError, match="model"):
        sessions.create_session("alice", alice_ws["id"], "do something", model={})
