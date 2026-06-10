"""Pure helpers for Shadow's MAGI deliberation engine.

The model-facing contract is intentionally strict. Provider output is untrusted
until it parses into the verdict schema below; the orchestrator owns one repair
attempt before declaring a MAGI unit malfunctioning.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any


MAGI_ROLES = [
    {
        "role": "melchior",
        "label": "MELCHIOR-01",
        "display": "MELCHIOR・01",
        "title": "Scientist",
        "persona": "Apply scientific rigor, evidence, falsifiability, and calibrated uncertainty.",
    },
    {
        "role": "balthasar",
        "label": "BALTHASAR-02",
        "display": "BALTHASAR・02",
        "title": "Guardian",
        "persona": "Apply protective pragmatism: safety, reliability, cost, reversibility, and the next useful action.",
    },
    {
        "role": "casper",
        "label": "CASPER-03",
        "display": "CASPER・03",
        "title": "Skeptic",
        "persona": "Challenge assumptions, test edge cases, and surface hidden failure modes without reflexive contrarianism.",
    },
]

VALID_STANCES = {"APPROVE", "REJECT", "CONDITIONAL"}
VERDICT_SCHEMA = (
    '{"stance":"APPROVE|REJECT|CONDITIONAL","answer":"useful answer or recommendation",'
    '"confidence":0.0,"reason":"one or two sentence justification",'
    '"risks":["optional concrete risk"],"dissent":"optional disagreement",'
    '"next_step":"optional next action"}'
)
JUDGE_SCHEMA = (
    '{"verdict":"APPROVE|REJECT|CONDITIONAL","final":"decisive final answer",'
    '"agreement_summary":"where the units agree",'
    '"dissent_summary":"where they differ and why"}'
)


class MagiParseError(ValueError):
    """A model response did not satisfy the MAGI structured contract."""


def role_by_key(key: str) -> dict[str, str]:
    key = (key or "").strip().lower()
    for role in MAGI_ROLES:
        if role["role"] == key:
            return role
    raise KeyError(key)


def normalize_stance(value: Any) -> str:
    raw = re.sub(r"[^A-Z]", "", str(value or "").strip().upper())
    aliases = {
        "YES": "APPROVE",
        "ALLOW": "APPROVE",
        "ACCEPT": "APPROVE",
        "GO": "APPROVE",
        "NO": "REJECT",
        "DENY": "REJECT",
        "BLOCK": "REJECT",
        "MAYBE": "CONDITIONAL",
        "CONDITION": "CONDITIONAL",
        "CONDITIONALAPPROVE": "CONDITIONAL",
    }
    return aliases.get(raw, raw)


def _json_from_text(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None
    candidates = [text]
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fence:
        candidates.insert(0, fence.group(1).strip())
    # The non-greedy candidates handle prose before/after a single JSON object.
    for match in re.finditer(r"\{[\s\S]*?\}", text):
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MagiParseError("confidence must be a number from 0 to 1") from exc
    # Be defensive with models that return percentages despite the schema.
    if 1 < number <= 100:
        number /= 100
    if not 0 <= number <= 1:
        raise MagiParseError("confidence must be between 0 and 1")
    return round(number, 4)


def confidence_percent(value: Any) -> int | None:
    if not isinstance(value, (int, float)):
        return None
    return round(max(0.0, min(1.0, float(value))) * 100)


def _list_from_value(value: Any, *, limit: int = 5) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        items = re.split(r"\n+|;", value)
    else:
        items = [value]
    out: list[str] = []
    for item in items:
        text = str(item or "").strip().strip("-*").strip()
        if text:
            out.append(text[:240])
        if len(out) >= limit:
            break
    return out


def _required_text(data: dict[str, Any], key: str, *aliases: str, limit: int = 12000) -> str:
    value: Any = data.get(key)
    if value is None:
        for alias in aliases:
            if data.get(alias) is not None:
                value = data.get(alias)
                break
    text = str(value or "").strip()
    if not text:
        raise MagiParseError(f"{key} is required")
    return text[:limit]


def parse_magi_response(raw: str) -> dict[str, Any]:
    """Parse and validate one unit's verdict.

    JSON may be fenced or surrounded by short prose, but all required fields
    must be present. Invalid output is never silently treated as a valid answer.
    """
    data = _json_from_text(raw)
    if data is None:
        raise MagiParseError("response is not a JSON object")
    stance = normalize_stance(data.get("stance") or data.get("verdict"))
    if stance not in VALID_STANCES:
        raise MagiParseError("stance must be APPROVE, REJECT, or CONDITIONAL")
    answer = _required_text(data, "answer", "response", "recommendation")
    reason = _required_text(data, "reason", "justification", "reasoning", limit=1000)
    return {
        "stance": stance,
        "answer": answer,
        "confidence": _confidence(data.get("confidence")),
        "reason": reason,
        "risks": _list_from_value(data.get("risks") or data.get("risk")),
        "dissent": str(data.get("dissent") or data.get("disagreement") or "").strip()[:500],
        "next_step": str(data.get("next_step") or data.get("next") or "").strip()[:500],
    }


def parse_judge_response(raw: str) -> dict[str, str]:
    data = _json_from_text(raw)
    if data is None:
        raise MagiParseError("judge response is not a JSON object")
    verdict = normalize_stance(data.get("verdict") or data.get("stance"))
    if verdict not in VALID_STANCES:
        raise MagiParseError("judge verdict must be APPROVE, REJECT, or CONDITIONAL")
    return {
        "verdict": verdict,
        "final": _required_text(data, "final", "answer"),
        "agreement_summary": _required_text(data, "agreement_summary", "agreement", limit=1200),
        "dissent_summary": _required_text(data, "dissent_summary", "dissent", limit=1200),
    }


def agreement_state(magi: list[dict[str, Any]]) -> str:
    answered = [item for item in magi if item.get("ok") and normalize_stance(item.get("stance")) in VALID_STANCES]
    if not answered:
        return "malfunction"
    if len(answered) == 1:
        return "insufficient"
    counts = Counter(normalize_stance(item.get("stance")) for item in answered)
    if len(counts) == 1:
        return "unanimous"
    top_count = counts.most_common(1)[0][1]
    if top_count > len(answered) / 2:
        return "majority"
    return "deadlock"


def _best_item(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not items:
        return None
    return max(items, key=lambda item: float(item.get("confidence") or 0))


def vote_resolution(magi: list[dict[str, Any]]) -> dict[str, Any]:
    answered = [item for item in magi if item.get("ok") and normalize_stance(item.get("stance")) in VALID_STANCES]
    failed = [item for item in magi if not item.get("ok")]
    agreement = agreement_state(magi)
    counts = Counter(normalize_stance(item.get("stance")) for item in answered)
    confidences = [float(item["confidence"]) for item in answered if isinstance(item.get("confidence"), (int, float))]
    avg_confidence = round(sum(confidences) / len(confidences), 4) if confidences else None

    if not answered:
        return {
            "final": "MAGI SYSTEM MALFUNCTION: no unit returned a valid verdict.",
            "winner": None,
            "counts": {},
            "agreement": "malfunction",
            "avg_confidence": None,
            "dissent": [],
            "requires_judge": False,
        }

    top_stance, top_count = counts.most_common(1)[0]
    winner = top_stance if agreement in {"unanimous", "majority"} else None
    winning_items = [item for item in answered if normalize_stance(item.get("stance")) == winner]
    selected = _best_item(winning_items)
    dissent = [
        {
            "role": item.get("role"),
            "label": item.get("label"),
            "stance": normalize_stance(item.get("stance")),
            "reason": item.get("reason") or item.get("dissent") or item.get("answer") or "",
        }
        for item in answered
        if winner and normalize_stance(item.get("stance")) != winner
    ]
    split = " / ".join(f"{stance} {count}" for stance, count in sorted(counts.items()))
    degraded = f" {len(failed)} unit(s) malfunctioned." if failed else ""

    if winner and selected:
        dissent_text = ""
        if dissent:
            dissent_text = "\n\nDissent: " + "; ".join(
                f"{item['label']} {item['stance']} - {item['reason']}" for item in dissent
            )
        final = (
            f"{winner}: {selected.get('answer', '').strip()}\n\n"
            f"Vote: {split}. {agreement.upper()}.{degraded}"
            f"{dissent_text}"
        )
    elif agreement == "insufficient":
        selected = answered[0]
        final = (
            f"INSUFFICIENT QUORUM: {selected.get('answer', '').strip()}\n\n"
            f"Only {selected.get('label') or selected.get('role')} returned a valid verdict."
        )
    else:
        final = f"DEADLOCK: no majority verdict. Vote: {split}.{degraded}"

    return {
        "final": final,
        "winner": winner,
        "counts": dict(counts),
        "agreement": agreement,
        "avg_confidence": avg_confidence,
        "dissent": dissent,
        "dissent_count": len(dissent),
        "requires_judge": agreement in {"deadlock", "insufficient"},
        "top_count": top_count,
        "answered_count": len(answered),
    }


def build_role_messages(query: str, role: dict[str, str]) -> list[dict[str, str]]:
    system = (
        f"You are {role['label']} ({role['title']}) in Shadow's MAGI system. "
        f"Decision lens: {role['persona']} "
        "Use this as a light analytical lens, not roleplay. Answer usefully first, then assign a verdict. "
        "Return one JSON object only. No markdown, preface, or trailing text. Required schema: "
        f"{VERDICT_SCHEMA}. Confidence is a decimal from 0 to 1. "
        "Give a clear verdict even when evidence is incomplete; use CONDITIONAL and state the condition."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": query}]


def build_repair_messages(
    original_messages: list[dict[str, str]],
    raw: str,
    error: str,
    *,
    judge: bool = False,
) -> list[dict[str, str]]:
    schema = JUDGE_SCHEMA if judge else VERDICT_SCHEMA
    return [
        *original_messages,
        {"role": "assistant", "content": (raw or "")[:12000]},
        {
            "role": "user",
            "content": (
                f"Your response was invalid: {error}. Return ONLY one valid JSON object using this exact schema: "
                f"{schema}. Do not add markdown fences or commentary."
            ),
        },
    ]


def build_debate_messages(query: str, magi: list[dict[str, Any]], role: dict[str, str]) -> list[dict[str, str]]:
    peers = []
    for item in magi:
        if item.get("role") == role["role"] or not item.get("ok"):
            continue
        peers.append(
            f"{item.get('label')}: stance={item.get('stance')}; confidence={item.get('confidence')}; "
            f"answer={item.get('answer')}; reason={item.get('reason')}; risks={item.get('risks') or []}"
        )
    system = (
        f"You are {role['label']} in MAGI peer review. {role['persona']} "
        "Re-evaluate your original conclusion against the peer verdicts. Change it only when their evidence warrants it. "
        "Return one JSON object only using this schema: "
        f"{VERDICT_SCHEMA}."
    )
    user = (
        f"Original request:\n{query}\n\n"
        "Peer MAGI responses:\n" + ("\n".join(peers) if peers else "No peer verdict survived.")
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_judge_messages(query: str, magi: list[dict[str, Any]]) -> list[dict[str, str]]:
    parts = []
    for item in magi:
        if item.get("ok"):
            parts.append(
                f"{item.get('label')} [{item.get('stance')} @ {item.get('confidence')}]\n"
                f"Answer: {item.get('answer')}\nReason: {item.get('reason')}\n"
                f"Risks: {item.get('risks') or []}\nDissent: {item.get('dissent') or ''}"
            )
        else:
            parts.append(f"{item.get('label')} [MALFUNCTION]\nError: {item.get('error') or 'unknown'}")
    system = (
        "You are the MAGI judge. Resolve the valid unit verdicts into one decisive answer without hiding dissent. "
        "Do not count a malfunctioning unit as a vote. Return one JSON object only using this schema: "
        f"{JUDGE_SCHEMA}."
    )
    user = f"Original request:\n{query}\n\nMAGI unit reports:\n\n" + "\n\n".join(parts)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def format_magi_markdown(payload: dict[str, Any]) -> str:
    vote = payload.get("vote") or {}
    lines = [
        f"## MAGI Verdict ({str(payload.get('resolution') or payload.get('mode') or 'vote').upper()})",
        "",
        str(payload.get("final") or "").strip(),
        "",
        f"Agreement: **{str(payload.get('agreement') or 'deadlock').upper()}**",
    ]
    avg = confidence_percent(vote.get("avg_confidence"))
    if avg is not None:
        lines.append(f"Average confidence: **{avg}%**")
    if payload.get("degraded"):
        lines.append("Status: **DEGRADED**")
    lines.append("")
    for item in payload.get("magi", []):
        status = "MALFUNCTION" if not item.get("ok") else str(item.get("status") or "ANSWERED").upper()
        confidence = confidence_percent(item.get("confidence"))
        lines.extend([
            f"### {item.get('display') or item.get('label')} - {item.get('stance', 'ERROR')} ({status})",
            f"Model: `{item.get('model', 'unknown')}`",
            f"Confidence: `{confidence if confidence is not None else 'n/a'}%`",
            "",
            str(item.get("answer") or item.get("error") or "").strip(),
            "",
        ])
        if item.get("reason"):
            lines.append("Reason: " + str(item.get("reason")))
        if item.get("risks"):
            lines.append("Risks: " + "; ".join(str(value) for value in item["risks"]))
        if item.get("dissent"):
            lines.append("Dissent: " + str(item.get("dissent")))
        if item.get("next_step"):
            lines.append("Next step: " + str(item.get("next_step")))
        lines.append("")
    return "\n".join(lines).strip()
