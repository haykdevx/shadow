"""Mission engine: workspace isolation, plan validation, concurrent
execution, approval pause/resume, retries, partial failure, budgets,
crash recovery, and rollback orchestration — all with a fake model and a
fake device dispatcher (no network, no real device).
"""

import asyncio
import json
import time

import pytest

import src.mission_engine as engine
import src.mission_policy as policy
import src.mission_workspaces as workspaces
from src.mission_engine import MissionError
from src.mission_policy import GrantStore
from src.mission_workspaces import WorkspaceError


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "MISSIONS_DIR", tmp_path / "missions")
    monkeypatch.setattr(workspaces, "DATA_DIR", tmp_path)
    monkeypatch.setattr(workspaces, "WORKSPACES_PATH", tmp_path / "workspaces.json")
    monkeypatch.setattr(policy, "POLICY_DIR", tmp_path)
    monkeypatch.setattr(policy, "RULES_PATH", tmp_path / "rules.json")
    monkeypatch.setattr(policy, "FULL_ACCESS_PATH", tmp_path / "full.json")
    monkeypatch.setattr(policy, "AUDIT_PATH", tmp_path / "audit.log")
    monkeypatch.setattr(policy, "GRANTS", GrantStore())
    engine._RUNNERS.clear()

    def fake_get_device(owner, device_id=None):
        if device_id == "dev-alice" and owner == "alice":
            return {"id": "dev-alice", "name": "Alice PC", "online": True, "transport": "relay"}
        if device_id == "dev-bob" and owner == "bob":
            return {"id": "dev-bob", "name": "Bob PC", "online": True, "transport": "relay"}
        raise __import__("src.shadow_devices", fromlist=["x"]).ShadowDeviceError(
            "That device does not belong to your account")
    monkeypatch.setattr("src.shadow_devices.get_device", fake_get_device)
    yield


@pytest.fixture()
def alice_ws():
    return workspaces.create_workspace("alice", "dev-alice", "/home/alice/proj", "proj")


def fake_dispatcher(script=None):
    """Replacement for shadow_devices.dispatch_action recording every call."""
    calls = []

    def dispatch_action(owner, device_id, action, args, *, confirmed, timeout=25):
        calls.append({"owner": owner, "device": device_id, "action": action,
                      "args": args, "confirmed": confirmed})
        if script:
            handler = script.get(action)
            if callable(handler):
                return handler(args)
            if handler is not None:
                return handler
        return {"ok": True}
    dispatch_action.calls = calls
    return dispatch_action


# ── workspace registry isolation ─────────────────────────────────────────

def test_workspace_cross_user_isolation(alice_ws):
    with pytest.raises(WorkspaceError, match="does not belong"):
        workspaces.get_workspace("bob", alice_ws["id"])
    with pytest.raises(WorkspaceError, match="does not belong"):
        workspaces.remove_workspace("bob", alice_ws["id"])


def test_workspace_cross_device_isolation():
    # bob cannot authorize a workspace on alice's device
    import src.shadow_devices as devices
    with pytest.raises(devices.ShadowDeviceError):
        workspaces.create_workspace("bob", "dev-alice", "/home/alice/proj")


def test_dispatch_sends_workspace_root_and_confirmed(monkeypatch, alice_ws):
    dispatcher = fake_dispatcher()
    monkeypatch.setattr("src.shadow_devices.dispatch_action", dispatcher)
    workspaces.dispatch("alice", alice_ws["id"], "ws_read", {"path": "a.py"})
    call = dispatcher.calls[0]
    assert call["args"]["roots"] == ["/home/alice/proj"]
    assert call["confirmed"] is True
    assert call["device"] == "dev-alice"


def test_dispatch_outside_workspace_path_needs_approval(monkeypatch, alice_ws):
    monkeypatch.setattr("src.shadow_devices.dispatch_action", fake_dispatcher())
    from src.mission_workspaces import WorkspaceApprovalRequired
    with pytest.raises(WorkspaceApprovalRequired):
        workspaces.dispatch("alice", alice_ws["id"], "ws_read", {"path": "/etc/passwd"})
    with pytest.raises(WorkspaceApprovalRequired):
        workspaces.dispatch("alice", alice_ws["id"], "ws_read", {"path": "../sibling/file"})


def test_dispatch_dangerous_command_needs_approval(monkeypatch, alice_ws):
    monkeypatch.setattr("src.shadow_devices.dispatch_action", fake_dispatcher())
    from src.mission_workspaces import WorkspaceApprovalRequired
    with pytest.raises(WorkspaceApprovalRequired):
        workspaces.dispatch("alice", alice_ws["id"], "ws_run", {"command": "sudo reboot"})


# ── mission creation + plan validation ───────────────────────────────────

ROLE = {"endpoint_id": "ep1", "model": "test-model"}
ALL_ROLES = {r: ROLE for r in engine.ROLES}


def make_mission(alice_ws, **kw):
    return engine.create_mission("alice", alice_ws["id"], "Fix the failing test in module X",
                                 roles=ALL_ROLES, **kw)


def test_mission_cross_user_isolation(alice_ws):
    mission = make_mission(alice_ws)
    with pytest.raises(MissionError, match="does not belong"):
        engine.load_mission("bob", mission["id"])


def test_validate_plan_rejects_cycles():
    with pytest.raises(ValueError, match="cycle"):
        engine._validate_plan([
            {"id": "a", "role": "implementer", "depends_on": ["b"]},
            {"id": "b", "role": "implementer", "depends_on": ["a"]},
        ])


def test_validate_plan_rejects_unknown_dependency():
    with pytest.raises(ValueError, match="unknown"):
        engine._validate_plan([{"id": "a", "role": "tester", "depends_on": ["ghost"]}])


def test_validate_plan_rejects_bad_roles_and_dup_ids():
    with pytest.raises(ValueError, match="unknown role"):
        engine._validate_plan([{"id": "a", "role": "wizard"}])
    with pytest.raises(ValueError, match="duplicate"):
        engine._validate_plan([{"id": "a", "role": "tester"}, {"id": "a", "role": "tester"}])


def test_planner_clarification_path(monkeypatch, alice_ws):
    mission = make_mission(alice_ws)

    async def fake_llm(m, role, messages, **kw):
        return json.dumps({"clarification": "Which installer, MSI or EXE?"})
    monkeypatch.setattr(engine, "_llm", fake_llm)
    monkeypatch.setattr(engine, "_repo_context", _async_const("ctx"))
    asyncio.run(engine.plan_mission(mission))
    assert mission["status"] == "clarifying"
    assert "MSI" in mission["clarification"]

    async def fake_llm2(m, role, messages, **kw):
        assert any("EXE installer" in (msg.get("content") or "") for msg in messages)
        return json.dumps({"clarification": None, "tasks": [
            {"id": "t1", "title": "fix", "role": "implementer", "goal": "fix it"}]})
    monkeypatch.setattr(engine, "_llm", fake_llm2)
    asyncio.run(engine.plan_mission(mission, answer="EXE installer"))
    assert mission["status"] == "ready"
    assert len(mission["tasks"]) == 1


def test_planner_repair_then_failure(monkeypatch, alice_ws):
    mission = make_mission(alice_ws)

    async def bad_llm(m, role, messages, **kw):
        return "I think we should... (no json)"
    monkeypatch.setattr(engine, "_llm", bad_llm)
    monkeypatch.setattr(engine, "_repo_context", _async_const("ctx"))
    with pytest.raises(MissionError, match="no valid plan"):
        asyncio.run(engine.plan_mission(mission))


def _async_const(value):
    async def inner(*args, **kwargs):
        return value
    return inner


# ── execution ────────────────────────────────────────────────────────────

def scripted_worker(script_by_role):
    """Fake _llm: pops the next scripted reply for the calling role."""
    async def fake_llm(mission, role, messages, **kw):
        replies = script_by_role.get(role) or []
        if not replies:
            return json.dumps({"done": True, "summary": f"{role} idle-done"})
        return replies.pop(0)
    return fake_llm


def run_mission(monkeypatch, alice_ws, tasks, llm, dispatcher=None, **mission_kw):
    mission = make_mission(alice_ws, **mission_kw)
    mission["tasks"] = engine._validate_plan(tasks)
    mission["status"] = "ready"
    engine._save_mission(mission)
    monkeypatch.setattr(engine, "_llm", llm)
    monkeypatch.setattr("src.shadow_devices.dispatch_action", dispatcher or fake_dispatcher())

    async def drive():
        engine.start_mission("alice", mission["id"])
        await engine._RUNNERS[mission["id"]]["task"]
    asyncio.run(drive())
    return engine.load_mission("alice", mission["id"])


def test_simple_mission_completes(monkeypatch, alice_ws):
    llm = scripted_worker({
        "implementer": [
            json.dumps({"tool": "ws_read", "args": {"path": "a.py"}}),
            json.dumps({"done": True, "summary": "patched a.py"}),
        ],
        "reviewer": [json.dumps({"done": True, "summary": "report"})],
    })
    result = run_mission(monkeypatch, alice_ws, [
        {"id": "t1", "title": "fix", "role": "implementer", "goal": "fix"},
    ], llm)
    assert result["status"] == "completed"
    assert result["tasks"][0]["status"] == "done"
    assert "a.py" in result["files_read"]
    assert result["report"]


def test_concurrent_independent_tasks(monkeypatch, alice_ws):
    started = []

    async def llm(mission, role, messages, **kw):
        first_user = messages[1]["content"]
        task_id = "t1" if "(t1)" in first_user else "t2"
        started.append((task_id, time.monotonic()))
        await asyncio.sleep(0.15)
        return json.dumps({"done": True, "summary": f"{task_id} ok"})

    result = run_mission(monkeypatch, alice_ws, [
        {"id": "t1", "title": "a", "role": "implementer", "goal": "g"},
        {"id": "t2", "title": "b", "role": "implementer", "goal": "g"},
    ], llm)
    assert result["status"] == "completed"
    assert {tid for tid, _ in started} == {"t1", "t2"}
    # both started within one batch (concurrent), not serially after sleep
    times = sorted(ts for _, ts in started)
    assert times[1] - times[0] < 0.12


def test_dependencies_gate_execution_order(monkeypatch, alice_ws):
    order = []

    async def llm(mission, role, messages, **kw):
        if role == "reviewer" and "MISSION RECORD" in messages[-1]["content"]:
            return "final report"  # _final_review call, not a task
        task_id = "t1" if "(t1)" in messages[1]["content"] else "t2"
        order.append(task_id)
        return json.dumps({"done": True, "summary": "ok"})

    result = run_mission(monkeypatch, alice_ws, [
        {"id": "t2", "title": "second", "role": "tester", "goal": "g", "depends_on": ["t1"]},
        {"id": "t1", "title": "first", "role": "implementer", "goal": "g"},
    ], llm)
    assert order == ["t1", "t2"]
    # dependent task saw the prerequisite's result in its prompt
    assert result["status"] == "completed"


def test_partial_failure_skips_dependents_not_mission(monkeypatch, alice_ws):
    llm = scripted_worker({
        "implementer": [json.dumps({"fail": "cannot do it"})] * 4,
        "tester": [json.dumps({"done": True, "summary": "independent ok"})],
        "reviewer": [json.dumps({"done": True, "summary": "report"})],
    })
    result = run_mission(monkeypatch, alice_ws, [
        {"id": "t1", "title": "impl", "role": "implementer", "goal": "g"},
        {"id": "t2", "title": "dependent", "role": "implementer", "goal": "g", "depends_on": ["t1"]},
        {"id": "t3", "title": "independent", "role": "tester", "goal": "g"},
    ], llm)
    statuses = {t["id"]: t["status"] for t in result["tasks"]}
    assert statuses == {"t1": "failed", "t2": "skipped", "t3": "done"}
    assert result["status"] == "completed_with_failures"


def test_recoverable_failure_retries_bounded(monkeypatch, alice_ws):
    attempts = []

    async def llm(mission, role, messages, **kw):
        if role != "implementer":
            return json.dumps({"done": True, "summary": "r"})
        attempts.append(1)
        raise MissionError("transient provider error")

    result = run_mission(monkeypatch, alice_ws, [
        {"id": "t1", "title": "impl", "role": "implementer", "goal": "g"},
    ], llm)
    assert result["tasks"][0]["status"] == "failed"
    assert result["tasks"][0]["attempts"] == engine.MAX_TASK_ATTEMPTS
    assert len(attempts) == engine.MAX_TASK_ATTEMPTS


def test_malformed_model_output_is_reprompted(monkeypatch, alice_ws):
    llm = scripted_worker({
        "implementer": [
            "definitely not json",
            json.dumps({"tool": "made_up_tool", "args": {}}),
            json.dumps({"done": True, "summary": "ok after correction"}),
        ],
    })
    result = run_mission(monkeypatch, alice_ws, [
        {"id": "t1", "title": "impl", "role": "implementer", "goal": "g"},
    ], llm)
    assert result["tasks"][0]["status"] == "done"


def test_mutations_create_checkpoint_first(monkeypatch, alice_ws):
    dispatcher = fake_dispatcher({"ws_checkpoint": {"ok": True, "git": None},
                                  "ws_write": {"ok": True, "sha256": "x"}})
    llm = scripted_worker({
        "implementer": [
            json.dumps({"tool": "ws_write", "args": {"path": "a.py", "text": "x"}}),
            json.dumps({"done": True, "summary": "wrote"}),
        ],
    })
    result = run_mission(monkeypatch, alice_ws, [
        {"id": "t1", "title": "impl", "role": "implementer", "goal": "g"},
    ], llm, dispatcher=dispatcher)
    actions = [c["action"] for c in dispatcher.calls]
    assert actions.index("ws_checkpoint") < actions.index("ws_write")
    write_call = next(c for c in dispatcher.calls if c["action"] == "ws_write")
    assert write_call["args"]["checkpoint_id"] == f"m-{result['id']}"
    assert result["checkpoint"]["id"] == f"m-{result['id']}"
    assert "a.py" in result["files_changed"]


def test_llm_budget_enforced(monkeypatch, alice_ws):
    mission = make_mission(alice_ws)
    mission["usage"]["llm_calls"] = engine.MAX_LLM_CALLS
    monkeypatch.setattr(engine, "_resolve_role_target",
                        lambda m, r: {"url": "http://x", "model": "m", "headers": {}})
    with pytest.raises(MissionError, match="budget"):
        asyncio.run(engine._llm(mission, "planner", [{"role": "user", "content": "x"}]))


def test_action_budget_enforced(monkeypatch, alice_ws):
    monkeypatch.setattr("src.shadow_devices.dispatch_action", fake_dispatcher())
    mission = make_mission(alice_ws)
    mission["usage"]["actions"] = engine.MAX_ACTIONS
    with pytest.raises(MissionError, match="budget"):
        asyncio.run(engine._dispatch(mission, "ws_read", {"path": "a"}, task_id="t"))


# ── approvals: pause / resume / decline / stop ───────────────────────────

def test_approval_pause_and_grant_resume(monkeypatch, alice_ws):
    """A gated command pauses the mission durably; an interactive approval
    (allow for this mission) lets the same task continue and finish."""
    llm = scripted_worker({
        "implementer": [
            json.dumps({"tool": "ws_run", "args": {"command": "pip install requests"}}),
            json.dumps({"done": True, "summary": "installed + done"}),
        ],
    })
    mission = make_mission(alice_ws)
    mission["tasks"] = engine._validate_plan([
        {"id": "t1", "title": "impl", "role": "implementer", "goal": "g"}])
    mission["status"] = "ready"
    engine._save_mission(mission)
    monkeypatch.setattr(engine, "_llm", llm)
    monkeypatch.setattr("src.shadow_devices.dispatch_action", fake_dispatcher())

    async def drive():
        engine.start_mission("alice", mission["id"])
        runner = engine._RUNNERS[mission["id"]]["task"]
        for _ in range(100):
            await asyncio.sleep(0.05)
            current = engine.load_mission("alice", mission["id"])
            pending = [a for a in current["approvals"] if a["status"] == "pending"]
            if pending:
                assert current["status"] == "paused_approval"
                engine.resolve_approval("alice", mission["id"], pending[0]["id"], "allow_mission")
                break
        else:
            raise AssertionError("approval never appeared")
        await asyncio.wait_for(runner, timeout=10)
    asyncio.run(drive())
    final = engine.load_mission("alice", mission["id"])
    assert final["status"] == "completed"
    assert final["tasks"][0]["status"] == "done"
    assert final["approvals"][0]["status"] == "approved"
    assert final["approvals"][0]["scope"] == "mission"


def test_approval_decline_lets_model_adapt(monkeypatch, alice_ws):
    seen_denials = []

    async def llm(mission, role, messages, **kw):
        last = messages[-1]["content"]
        if "DENIED BY USER" in last:
            seen_denials.append(last)
            return json.dumps({"done": True, "summary": "finished without install"})
        if role == "implementer" and len(messages) == 2:
            return json.dumps({"tool": "ws_run", "args": {"command": "pip install x"}})
        return json.dumps({"done": True, "summary": "ok"})

    mission = make_mission(alice_ws)
    mission["tasks"] = engine._validate_plan([
        {"id": "t1", "title": "impl", "role": "implementer", "goal": "g"}])
    mission["status"] = "ready"
    engine._save_mission(mission)
    monkeypatch.setattr(engine, "_llm", llm)
    monkeypatch.setattr("src.shadow_devices.dispatch_action", fake_dispatcher())

    async def drive():
        engine.start_mission("alice", mission["id"])
        runner = engine._RUNNERS[mission["id"]]["task"]
        for _ in range(100):
            await asyncio.sleep(0.05)
            current = engine.load_mission("alice", mission["id"])
            pending = [a for a in current["approvals"] if a["status"] == "pending"]
            if pending:
                engine.resolve_approval("alice", mission["id"], pending[0]["id"], "decline")
                break
        await asyncio.wait_for(runner, timeout=10)
    asyncio.run(drive())
    final = engine.load_mission("alice", mission["id"])
    assert seen_denials, "model never saw the denial"
    assert final["tasks"][0]["status"] == "done"


def test_approval_cannot_be_resolved_cross_user(monkeypatch, alice_ws):
    mission = make_mission(alice_ws)
    mission["approvals"] = [{"id": "ap1", "status": "pending", "grant_key": "k",
                             "summary": "s", "task_id": "t1"}]
    engine._save_mission(mission)
    with pytest.raises(MissionError, match="does not belong"):
        engine.resolve_approval("bob", mission["id"], "ap1", "allow_once")


# ── crash recovery ───────────────────────────────────────────────────────

def test_recover_missions_after_restart(alice_ws):
    mission = make_mission(alice_ws)
    mission["status"] = "running"
    mission["tasks"] = engine._validate_plan([
        {"id": "t1", "title": "a", "role": "implementer", "goal": "g"}])
    mission["tasks"][0]["status"] = "running"
    engine._save_mission(mission)

    recovered = engine.recover_missions()
    assert recovered == 1
    fresh = engine.load_mission("alice", mission["id"])
    assert fresh["status"] == "paused"
    assert fresh["tasks"][0]["status"] == "ready"
    assert any(e["kind"] == "recovered" for e in fresh["events"])


def test_resume_after_recovery_runs_to_completion(monkeypatch, alice_ws):
    mission = make_mission(alice_ws)
    mission["status"] = "running"
    mission["tasks"] = engine._validate_plan([
        {"id": "t1", "title": "a", "role": "implementer", "goal": "g"}])
    mission["tasks"][0]["status"] = "running"
    engine._save_mission(mission)
    engine.recover_missions()

    llm = scripted_worker({"implementer": [json.dumps({"done": True, "summary": "ok"})]})
    monkeypatch.setattr(engine, "_llm", llm)
    monkeypatch.setattr("src.shadow_devices.dispatch_action", fake_dispatcher())

    async def drive():
        engine.resume_mission("alice", mission["id"])
        await asyncio.wait_for(engine._RUNNERS[mission["id"]]["task"], timeout=10)
    asyncio.run(drive())
    assert engine.load_mission("alice", mission["id"])["status"] == "completed"


# ── rollback ─────────────────────────────────────────────────────────────

def test_rollback_full_and_per_file(monkeypatch, alice_ws):
    dispatcher = fake_dispatcher({
        "ws_restore": lambda args: {"ok": True, "restored": args.get("paths") or ["a.py", "b.py"],
                                    "removed_created": []},
    })
    monkeypatch.setattr("src.shadow_devices.dispatch_action", dispatcher)
    mission = make_mission(alice_ws)
    mission["status"] = "completed"
    mission["checkpoint"] = {"id": f"m-{mission['id']}", "git": None}
    engine._save_mission(mission)

    result = asyncio.run(engine.rollback_mission("alice", mission["id"], paths=["a.py"]))
    assert result["restored"] == ["a.py"]
    assert engine.load_mission("alice", mission["id"])["status"] == "completed"  # per-file ≠ full

    asyncio.run(engine.rollback_mission("alice", mission["id"]))
    assert engine.load_mission("alice", mission["id"])["status"] == "rolled_back"
    restore_calls = [c for c in dispatcher.calls if c["action"] == "ws_restore"]
    assert restore_calls[0]["args"]["checkpoint_id"] == f"m-{mission['id']}"


def test_rollback_requires_ownership(alice_ws):
    mission = make_mission(alice_ws)
    with pytest.raises(MissionError, match="does not belong"):
        asyncio.run(engine.rollback_mission("bob", mission["id"]))
