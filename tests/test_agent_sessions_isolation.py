"""Per-run isolation + server-enforced read-only intent.

Regression coverage for the bugs described as: stale changed-file reports for
read-only tasks, changed-file state leaking between follow-up runs, repeated
old answers, and stopped runs surfacing an unrelated previous answer. These
all trace back to two fixes in ``src.agent_sessions``:

1. Each ``_runner`` invocation is a distinct "run leg" (its own ``run_id``,
   ``run_seq``) that resets per-run state (``files_changed``, ``files_read``,
   ``commands``, ``terminal``, ``report``, ``error``) while accumulating
   ``files_changed_total`` and a capped ``history`` across the session.
2. ``classify_intent`` deterministically marks a run ``read_only`` or
   ``mutating``; when ``read_only`` the tool loop denies every tool in
   ``MUTATING_TOOLS`` server-side, before dispatch — no checkpoint, no
   ``files_changed`` mutation, no policy/approval interaction.
"""

import asyncio
import json

import pytest

import src.agent_sessions as sessions
import src.workspace_policy as policy
import src.workspace_service as workspaces
from src.agent_sessions import ProviderError
from src.workspace_policy import GrantStore


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


# ── classify_intent ──────────────────────────────────────────────────────

def test_classify_intent_mutating_verbs():
    assert sessions.classify_intent("Fix the bug in utils.py") == "mutating"
    assert sessions.classify_intent("Add a notes.md file with a TODO list") == "mutating"


def test_classify_intent_read_only_verbs():
    assert sessions.classify_intent("Analyze this codebase for security issues") == "read_only"
    assert sessions.classify_intent("What does the dispatch function do?") == "read_only"
    assert sessions.classify_intent("Explain how the agent loop works") == "read_only"


def test_classify_intent_explicit_read_only_overrides_mutating_verb():
    # "fix" is a mutating verb, but the explicit no-changes instruction wins.
    assert sessions.classify_intent(
        "Review the auth module and fix the issues you find, "
        "but do not modify any files — analysis only") == "read_only"
    assert sessions.classify_intent("Please do a read-only review of src/") == "read_only"
    assert sessions.classify_intent(
        "Audit the repo without making any changes") == "read_only"


def test_classify_intent_ambiguous_defaults_to_mutating():
    assert sessions.classify_intent("asdf qwer zxcv") == "mutating"
    assert sessions.classify_intent("") == "mutating"


def test_mutating_and_read_only_tools_partition_workspace_actions():
    assert sessions.READ_ONLY_TOOLS | sessions.MUTATING_TOOLS == workspaces.WORKSPACE_ACTIONS
    assert sessions.READ_ONLY_TOOLS & sessions.MUTATING_TOOLS == set()
    assert len(sessions.READ_ONLY_TOOLS) == 9
    assert len(sessions.MUTATING_TOOLS) == 10


# ── server-enforced read-only intent ────────────────────────────────────

def test_read_only_task_denies_mutating_tool_before_dispatch(monkeypatch, alice_ws):
    dispatcher = fake_dispatcher()
    record = run(monkeypatch, make_session(
        alice_ws, "Analyze this codebase, do not modify any files."), scripted_llm([
            json.dumps({"tool": "ws_write", "args": {"path": "notes.md", "text": "hi"}}),
            json.dumps({"done": True, "report": "Reviewed the code; no changes made."}),
        ]), dispatcher)

    assert record["intent"] == "read_only"
    assert record["status"] == "completed"
    # Nothing was ever dispatched — not even a checkpoint.
    assert dispatcher.calls == []
    assert record["checkpoint"] is None
    assert record["files_changed"] == []
    assert any(e["kind"] == "tool_denied" for e in record["events"])
    assert any("DENIED: read-only run" in m["content"] for m in record["transcript"])


def test_read_only_task_still_allows_inspection_tools(monkeypatch, alice_ws):
    dispatcher = fake_dispatcher()
    record = run(monkeypatch, make_session(
        alice_ws, "Explain how utils.py works, read-only."), scripted_llm([
            json.dumps({"tool": "ws_read", "args": {"path": "utils.py"}}),
            json.dumps({"done": True, "report": "utils.py contains helper functions."}),
        ]), dispatcher)

    assert record["intent"] == "read_only"
    assert record["status"] == "completed"
    assert "utils.py" in record["files_read"]
    assert [c["action"] for c in dispatcher.calls] == ["ws_read"]


def test_read_only_system_prompt_addendum_present(monkeypatch, alice_ws):
    session = make_session(alice_ws, "Analyze this codebase, read-only review.")
    session["intent"] = sessions.classify_intent(sessions._trigger_text(session))
    msg = sessions._system_message(session)
    assert "READ-ONLY" in msg["content"]

    mutating_session = make_session(alice_ws, "Fix the bug in utils.py")
    mutating_session["intent"] = sessions.classify_intent(sessions._trigger_text(mutating_session))
    msg = sessions._system_message(mutating_session)
    assert "READ-ONLY" not in msg["content"]


# ── per-run isolation across follow-up legs ─────────────────────────────

def test_per_run_state_resets_while_total_accumulates(monkeypatch, alice_ws):
    session = make_session(alice_ws, "Create foo.py with a hello function")
    record = run(monkeypatch, session, scripted_llm([
        json.dumps({"tool": "ws_write", "args": {"path": "foo.py", "text": "def hello(): pass"}}),
        json.dumps({"done": True, "report": "Created foo.py"}),
    ]))
    assert record["status"] == "completed"
    assert record["files_changed"] == ["foo.py"]
    assert record["files_changed_total"] == ["foo.py"]
    first_run_id = record["run_id"]
    assert record["run_seq"] == 1

    # Follow-up: a different file. The new run leg must not carry over
    # files_changed/files_read/commands from the previous leg.
    sessions.send_message("alice", record["id"], "Now also create bar.py with a goodbye function")
    record = run(monkeypatch, record, scripted_llm([
        json.dumps({"tool": "ws_write", "args": {"path": "bar.py", "text": "def goodbye(): pass"}}),
        json.dumps({"done": True, "report": "Created bar.py"}),
    ]))

    assert record["status"] == "completed"
    assert record["run_seq"] == 2
    assert record["run_id"] != first_run_id
    # Per-run state reflects only THIS run leg's effects.
    assert record["files_changed"] == ["bar.py"]
    assert "foo.py" not in record["files_changed"]
    # Cumulative tracking accumulates across the whole session lifetime.
    assert record["files_changed_total"] == ["foo.py", "bar.py"]


def test_history_records_one_summary_per_run_leg(monkeypatch, alice_ws):
    session = make_session(alice_ws, "Analyze the repo, read-only.")
    record = run(monkeypatch, session, scripted_llm([
        json.dumps({"done": True, "report": "first analysis"}),
    ]))
    assert len(record["history"]) == 1
    first = record["history"][0]
    assert first["run_seq"] == 1
    assert first["intent"] == "read_only"
    assert first["status"] == "completed"
    assert first["report"] == "first analysis"

    sessions.send_message("alice", record["id"], "Now do another pass and summarize again")
    record = run(monkeypatch, record, scripted_llm([
        json.dumps({"done": True, "report": "second analysis"}),
    ]))
    assert len(record["history"]) == 2
    second = record["history"][1]
    assert second["run_seq"] == 2
    assert second["report"] == "second analysis"
    assert second["run_id"] != first["run_id"]
    # The first leg's summary is untouched by the second leg.
    assert record["history"][0]["report"] == "first analysis"


def test_followup_without_new_intent_keeps_prior_classification(monkeypatch, alice_ws):
    """A plain retry of an existing run leg (no new inbox message) must not
    silently flip a read-only run to mutating or vice versa."""
    session = make_session(alice_ws, "Analyze the repo, do not change any files.")
    record = run(monkeypatch, session, scripted_llm([ProviderError("503")]))
    assert record["status"] == "failed"
    assert record["intent"] == "read_only"

    # retry_session with no new inbox message: same trigger text, intent kept.
    async def drive():
        sessions.retry_session("alice", record["id"])
        await sessions._RUNNERS[record["id"]]["task"]
    monkeypatch.setattr(sessions, "_llm", scripted_llm([
        json.dumps({"done": True, "report": "analysis complete"}),
    ]))
    monkeypatch.setattr("src.shadow_devices.dispatch_action", fake_dispatcher())
    asyncio.run(drive())
    fresh = sessions.load_session("alice", record["id"])
    assert fresh["status"] == "completed"
    assert fresh["intent"] == "read_only"
    assert fresh["run_seq"] == 2


# ── staleness guard ───────────────────────────────────────────────────────

def test_save_if_current_rejects_late_write_from_superseded_run(monkeypatch, alice_ws):
    session = make_session(alice_ws, "Fix the bug in utils.py")

    # Run leg "A" finishes and persists run_id "run-A".
    session["run_id"] = "run-A"
    session["run_seq"] = 1
    session["status"] = "completed"
    session["report"] = "first answer"
    sessions._save(session)

    # A newer run leg "B" has since started and persisted its own state.
    newer = sessions.load_session("alice", session["id"])
    newer["run_id"] = "run-B"
    newer["run_seq"] = 2
    newer["status"] = "running"
    newer["report"] = ""
    sessions._save(newer)

    # The (cancelled/superseded) run-A handler now tries to write its
    # terminal state — this must be a no-op because run-B has taken over.
    stale = dict(session)
    stale["status"] = "stopped"
    stale["report"] = "stale answer from run A"
    sessions._save_if_current(stale, "run-A")

    fresh = sessions.load_session("alice", session["id"])
    assert fresh["run_id"] == "run-B"
    assert fresh["status"] == "running"
    assert fresh["report"] == ""


def test_save_if_current_writes_when_run_is_still_current(monkeypatch, alice_ws):
    session = make_session(alice_ws, "Fix the bug in utils.py")
    session["run_id"] = "run-A"
    session["run_seq"] = 1
    sessions._save(session)

    session["status"] = "completed"
    session["report"] = "done"
    sessions._save_if_current(session, "run-A")

    fresh = sessions.load_session("alice", session["id"])
    assert fresh["status"] == "completed"
    assert fresh["report"] == "done"


# ── events carry run_id ───────────────────────────────────────────────────

def test_events_are_tagged_with_run_id(monkeypatch, alice_ws):
    record = run(monkeypatch, make_session(alice_ws), scripted_llm([
        json.dumps({"done": True, "report": "done"}),
    ]))
    assert record["run_id"]
    # The "created" event predates any run leg (run_id was still "" then);
    # every event emitted once the runner started must carry this run's id.
    run_events = [e for e in record["events"] if e["kind"] != "created"]
    assert run_events
    assert all(e.get("run_id") == record["run_id"] for e in run_events)
