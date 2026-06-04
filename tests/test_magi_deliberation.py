import asyncio

from src.magi_deliberation import (
    MAGI_ROLES,
    agreement_state,
    build_debate_messages,
    parse_magi_response,
    vote_resolution,
)
from src.magi_orchestrator import MagiOrchestrator


def test_parse_magi_json_response():
    parsed = parse_magi_response('{"stance":"approve","confidence":82,"answer":"Ship it with tests.","risks":["regression"],"dissent":"watch CI","next_step":"run smoke"}')
    assert parsed["stance"] == "APPROVE"
    assert parsed["answer"] == "Ship it with tests."
    assert parsed["confidence"] == 82
    assert parsed["risks"] == ["regression"]
    assert parsed["dissent"] == "watch CI"
    assert parsed["next_step"] == "run smoke"


def test_parse_magi_fenced_json_response():
    parsed = parse_magi_response('```json\n{"stance":"conditional","answer":"Only after backup."}\n```')
    assert parsed["stance"] == "CONDITIONAL"
    assert parsed["answer"] == "Only after backup."
    assert parsed["confidence"] is None
    assert parsed["risks"] == []


def test_vote_resolution_majority_with_dissent():
    magi = [
        {"ok": True, "stance": "APPROVE", "answer": "yes", "confidence": 90, "dissent": ""},
        {"ok": True, "stance": "APPROVE", "answer": "yes", "confidence": 80, "dissent": ""},
        {"ok": True, "stance": "REJECT", "answer": "no", "confidence": 70, "dissent": "risk"},
    ]
    result = vote_resolution(magi)
    assert result["agreement"] == "majority"
    assert result["winner"] == "APPROVE"
    assert result["counts"] == {"APPROVE": 2, "REJECT": 1}
    assert result["avg_confidence"] == 80.0
    assert result["dissent_count"] == 1


def test_agreement_degraded_two_answered():
    magi = [
        {"ok": True, "stance": "CONDITIONAL", "answer": "guard it"},
        {"ok": True, "stance": "CONDITIONAL", "answer": "guard it"},
        {"ok": False, "stance": "ERROR", "error": "timeout"},
    ]
    assert agreement_state(magi) == "unanimous"
    result = vote_resolution(magi)
    assert "Degraded" in result["final"]


def test_build_debate_messages_excludes_current_role_and_requests_json():
    magi = [
        {"role": "melchior", "ok": True, "label": "MELCHIOR-01", "stance": "APPROVE", "answer": "ship", "risks": []},
        {"role": "balthasar", "ok": True, "label": "BALTHASAR-02", "stance": "REJECT", "answer": "backup first", "risks": ["data loss"]},
    ]
    messages = build_debate_messages("deploy?", magi, MAGI_ROLES[0])
    joined = "\n".join(m["content"] for m in messages)
    assert "BALTHASAR-02" in joined
    assert "MELCHIOR-01" not in joined.split("Peer MAGI responses:", 1)[-1]
    assert "strict JSON" in joined

def test_magi_orchestrator_degrades_when_one_role_fails():
    async def fake_call(endpoint, model, messages, **kwargs):
        if model == "bad-model":
            raise RuntimeError("provider timeout")
        return '{"stance":"APPROVE","confidence":80,"answer":"Proceed with guardrails.","risks":[]}'

    targets = [
        {"endpoint": "http://local/v1/chat/completions", "model": "good-a", "headers": {}, "endpoint_id": "a", "endpoint_name": "A"},
        {"endpoint": "http://local/v1/chat/completions", "model": "bad-model", "headers": {}, "endpoint_id": "b", "endpoint_name": "B"},
        {"endpoint": "http://local/v1/chat/completions", "model": "good-c", "headers": {}, "endpoint_id": "c", "endpoint_name": "C"},
    ]
    result = asyncio.run(MagiOrchestrator(fake_call).deliberate("ship?", targets, mode="vote", timeout_seconds=10))
    assert result["degraded"] is True
    assert result["agreement"] == "unanimous"
    assert result["vote"]["winner"] == "APPROVE"
    assert [item["status"] for item in result["magi"]].count("failed") == 1


def test_magi_orchestrator_judge_falls_back_to_vote():
    async def fake_call(endpoint, model, messages, **kwargs):
        if kwargs.get("prompt_type") == "magi-judge":
            raise RuntimeError("judge offline")
        return '{"stance":"CONDITIONAL","confidence":70,"answer":"Do it after backup.","risks":["rollback"]}'

    targets = [
        {"endpoint": "http://local/v1/chat/completions", "model": "a", "headers": {}, "endpoint_id": "a", "endpoint_name": "A"},
        {"endpoint": "http://local/v1/chat/completions", "model": "b", "headers": {}, "endpoint_id": "b", "endpoint_name": "B"},
        {"endpoint": "http://local/v1/chat/completions", "model": "c", "headers": {}, "endpoint_id": "c", "endpoint_name": "C"},
    ]
    result = asyncio.run(MagiOrchestrator(fake_call).deliberate("deploy?", targets, mode="judge", timeout_seconds=10))
    assert result["resolution"] == "vote_fallback"
    assert result["degraded"] is True
    assert result["vote"]["winner"] == "CONDITIONAL"
