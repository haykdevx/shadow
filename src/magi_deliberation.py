"""MAGI tri-model deliberation helpers.

Pure helpers live here so stance parsing and vote resolution can be tested
without a live model provider.
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
        "title": "Scientist",
        "persona": "logic-first scientist: precise, evidence-driven, explicit about uncertainty",
    },
    {
        "role": "balthasar",
        "label": "BALTHASAR-02",
        "title": "Guardian",
        "persona": "protective pragmatic operator: weighs safety, cost, reliability, and next action",
    },
    {
        "role": "casper",
        "label": "CASPER-03",
        "title": "Skeptic",
        "persona": "intuition-led skeptic: challenges assumptions, catches hidden failure modes",
    },
]

VALID_STANCES = {"APPROVE", "REJECT", "CONDITIONAL", "ANSWER"}


def role_by_key(key: str) -> dict[str, str]:
    key = (key or "").strip().lower()
    for role in MAGI_ROLES:
        if role["role"] == key:
            return role
    raise KeyError(key)


def normalize_stance(value: Any) -> str:
    raw = str(value or "").strip().upper()
    raw = re.sub(r"[^A-Z]", "", raw)
    aliases = {
        "YES": "APPROVE",
        "ALLOW": "APPROVE",
        "ACCEPT": "APPROVE",
        "NO": "REJECT",
        "DENY": "REJECT",
        "BLOCK": "REJECT",
        "MAYBE": "CONDITIONAL",
        "CONDITION": "CONDITIONAL",
        "CONDITIONALAPPROVE": "CONDITIONAL",
    }
    raw = aliases.get(raw, raw)
    return raw if raw in VALID_STANCES else "ANSWER"


def _json_from_text(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None
    candidates = [text]
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fence:
        candidates.insert(0, fence.group(1).strip())
    obj = re.search(r"\{[\s\S]*\}", text)
    if obj:
        candidates.append(obj.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return None


def _coerce_confidence(value: Any) -> int | None:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, n))


def _list_from_value(value: Any, *, limit: int = 5) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        items = re.split(r"\n+|;", value)
    else:
        items = [value]
    out = []
    for item in items:
        text = str(item or "").strip().strip("-*").strip()
        if text:
            out.append(text[:240])
        if len(out) >= limit:
            break
    return out


def parse_magi_response(raw: str) -> dict[str, Any]:
    """Return structured MAGI fields from permissive model output."""
    text = (raw or "").strip()
    data = _json_from_text(text)
    if data:
        stance = normalize_stance(data.get("stance") or data.get("verdict"))
        answer = str(data.get("answer") or data.get("reasoning") or data.get("response") or "").strip()
        if not answer:
            answer = text
        confidence = _coerce_confidence(data.get("confidence"))
        risks = _list_from_value(data.get("risks") or data.get("risk"))
        dissent = str(data.get("dissent") or data.get("disagreement") or "").strip()[:500]
        next_step = str(data.get("next_step") or data.get("next") or data.get("recommendation") or "").strip()[:500]
        return {
            "stance": stance,
            "answer": answer,
            "confidence": confidence,
            "risks": risks,
            "dissent": dissent,
            "next_step": next_step,
        }

    stance = "ANSWER"
    m = re.search(r"\b(APPROVE|REJECT|CONDITIONAL|ANSWER)\b", text, re.IGNORECASE)
    if m:
        stance = normalize_stance(m.group(1))
    return {
        "stance": stance,
        "answer": text,
        "confidence": None,
        "risks": [],
        "dissent": "",
        "next_step": "",
    }


def agreement_state(magi: list[dict[str, Any]]) -> str:
    answered = [m for m in magi if m.get("ok")]
    if len(answered) < 2:
        return "split"
    stances = [normalize_stance(m.get("stance")) for m in answered]
    counts = Counter(stances)
    if len(counts) == 1:
        return "unanimous"
    if counts.most_common(1)[0][1] >= 2:
        return "majority"
    return "split"


def vote_resolution(magi: list[dict[str, Any]]) -> dict[str, Any]:
    answered = [m for m in magi if m.get("ok")]
    failed = [m for m in magi if not m.get("ok")]
    confidence_values = [int(m["confidence"]) for m in answered if isinstance(m.get("confidence"), int)]
    avg_confidence = round(sum(confidence_values) / len(confidence_values), 1) if confidence_values else None
    dissent_count = sum(1 for m in answered if str(m.get("dissent") or "").strip())
    if not answered:
        return {
            "final": "MAGI could not reach a verdict because every role failed.",
            "winner": None,
            "counts": {},
            "agreement": "split",
            "avg_confidence": None,
            "dissent_count": 0,
        }

    counts = Counter(normalize_stance(m.get("stance")) for m in answered)
    top_stance, top_count = counts.most_common(1)[0]
    agreement = agreement_state(magi)
    split = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    confidence_note = f" Avg confidence: {avg_confidence}%." if avg_confidence is not None else ""
    dissent_note = f" Dissent notes: {dissent_count}." if dissent_count else ""
    degraded = f" Degraded: {len(failed)} role(s) failed." if failed else ""

    if agreement in {"unanimous", "majority"} and top_stance != "ANSWER":
        final = f"MAGI vote: {top_stance} ({top_count}/{len(answered)} answered). Split: {split}.{confidence_note}{dissent_note}{degraded}"
    elif agreement in {"unanimous", "majority"}:
        final = (
            f"MAGI consensus answer ({top_count}/{len(answered)} aligned as ANSWER)."
            f"{confidence_note}{dissent_note}{degraded}\n\n{answered[0].get('answer', '').strip()}"
        )
    else:
        final = (
            "MAGI split: no majority stance. Review the dissent before acting."
            f" Split: {split}.{confidence_note}{dissent_note}{degraded}"
        )
    return {
        "final": final,
        "winner": top_stance if agreement != "split" else None,
        "counts": dict(counts),
        "agreement": agreement,
        "avg_confidence": avg_confidence,
        "dissent_count": dissent_count,
    }


def build_role_messages(query: str, role: dict[str, str]) -> list[dict[str, str]]:
    system = (
        f"You are {role['label']} ({role['title']}), part of Shadow's MAGI deliberation panel. "
        f"Bias: {role['persona']}. Keep the bias light: improve judgment without roleplay. "
        "Answer independently. Return strict JSON only with keys: "
        '{"stance":"APPROVE|REJECT|CONDITIONAL|ANSWER","confidence":0-100,"answer":"...","risks":["..."],"dissent":"...","next_step":"..."}.\n'
        "Use APPROVE/REJECT/CONDITIONAL for decisions, plans, risky actions, or recommendations. "
        "Use ANSWER for open informational questions. Keep answer concise but useful. "
        "Risks should be concrete. Dissent is what you expect another MAGI role may miss."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": query}]


def build_debate_messages(query: str, magi: list[dict[str, Any]], role: dict[str, str]) -> list[dict[str, str]]:
    peer_lines = []
    for item in magi:
        if item.get("role") == role["role"] or not item.get("ok"):
            continue
        peer_lines.append(
            f"### {item.get('label') or item.get('role')}\n"
            f"Stance: {item.get('stance') or 'ANSWER'}\n"
            f"Confidence: {item.get('confidence') if item.get('confidence') is not None else 'n/a'}\n"
            f"Answer: {item.get('answer') or ''}\n"
            f"Risks: {', '.join(item.get('risks') or [])}"
        )
    system = (
        f"You are {role['label']} ({role['title']}) in Shadow's MAGI debate round. "
        f"Bias: {role['persona']}. Reassess your answer after seeing peer outputs. "
        "Return strict JSON only with keys: "
        '{"stance":"APPROVE|REJECT|CONDITIONAL|ANSWER","confidence":0-100,"answer":"...","risks":["..."],"dissent":"...","next_step":"..."}. '
        "Do not simply agree; update only if the peers exposed a real issue."
    )
    user = "Original request:\n" + query + "\n\nPeer MAGI responses:\n" + ("\n\n".join(peer_lines) or "No peer answers survived.")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_judge_messages(query: str, magi: list[dict[str, Any]]) -> list[dict[str, str]]:
    parts = []
    for item in magi:
        status = "OK" if item.get("ok") else "FAILED"
        answer = item.get("answer") or item.get("error") or ""
        parts.append(
            f"### {item.get('label') or item.get('role')} [{status}]"
            f"\nModel: {item.get('model') or 'unknown'}"
            f"\nStance: {item.get('stance') or 'ERROR'}"
            f"\n{answer}"
        )
    system = (
        "You are Shadow's MAGI judge synthesis layer. Produce the final answer from three independent MAGI roles. "
        "Cite where they agreed, where they diverged, and give one decisive final verdict or answer. "
        "Do not hide dissent. Be concise."
    )
    user = "Original request:\n" + query + "\n\nMAGI responses:\n\n" + "\n\n".join(parts)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def format_magi_markdown(payload: dict[str, Any]) -> str:
    lines = [
        f"## MAGI Verdict ({str(payload.get('mode', 'vote')).upper()})",
        "",
        str(payload.get("final") or "").strip(),
        "",
        f"Agreement: **{payload.get('agreement', 'split')}**",
    ]
    vote = payload.get("vote") or {}
    if vote.get("avg_confidence") is not None:
        lines.append(f"Average confidence: **{vote.get('avg_confidence')}%**")
    if payload.get("degraded"):
        lines.append("Status: **degraded**")
    lines.append("")
    for item in payload.get("magi", []):
        status = "answered" if item.get("ok") else "failed"
        confidence = item.get("confidence")
        lines.extend([
            f"### {item.get('label')} - {item.get('stance', 'ERROR')} ({status})",
            f"Model: `{item.get('model', 'unknown')}`",
            f"Confidence: `{confidence if confidence is not None else 'n/a'}`",
            "",
            str(item.get("answer") or item.get("error") or "").strip(),
            "",
        ])
        risks = item.get("risks") or []
        if risks:
            lines.append("Risks: " + "; ".join(str(r) for r in risks))
        if item.get("dissent"):
            lines.append("Dissent: " + str(item.get("dissent")))
        if item.get("next_step"):
            lines.append("Next step: " + str(item.get("next_step")))
        lines.append("")
    return "\n".join(lines).strip()
