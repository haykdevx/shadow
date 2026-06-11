"""Concurrent, resilient orchestration for Shadow's three MAGI units."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from src.magi_deliberation import (
    MAGI_ROLES,
    MagiParseError,
    build_debate_messages,
    build_judge_messages,
    build_repair_messages,
    build_role_messages,
    parse_judge_response,
    parse_magi_response,
    vote_resolution,
)

MagiCall = Callable[..., Awaitable[str]]
EventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


class MagiOrchestrator:
    """Run fan-out, peer review, voting, and judge synthesis."""

    def __init__(self, call_model: MagiCall | None = None, logger_: logging.Logger | None = None):
        self.call_model = call_model or self._default_call_model
        self.logger = logger_ or logging.getLogger(__name__)

    async def _emit(self, callback: EventCallback | None, event: dict[str, Any]) -> None:
        if callback is None:
            return
        try:
            result = callback(event)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("MAGI event callback failed: %s", exc)

    async def _structured_call(
        self,
        target: dict[str, Any],
        messages: list[dict[str, str]],
        *,
        parser: Callable[[str], dict[str, Any]],
        prompt_type: str,
        timeout_seconds: int,
        max_tokens: int,
        temperature: float,
        judge: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        """Call once, then re-ask once only when the structure is invalid."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        current_messages = messages
        repaired = False
        last_error: Exception | None = None

        for attempt in range(2):
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"{prompt_type} exceeded {timeout_seconds}s")
            raw = await asyncio.wait_for(
                self.call_model(
                    target["endpoint"],
                    target["model"],
                    current_messages,
                    headers=target.get("headers") or {},
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=max(1, int(remaining)),
                    max_retries=1,
                    prompt_type=prompt_type if attempt == 0 else f"{prompt_type}-repair",
                ),
                timeout=remaining,
            )
            try:
                return parser(raw), repaired
            except MagiParseError as exc:
                last_error = exc
                if attempt == 1:
                    break
                repaired = True
                current_messages = build_repair_messages(messages, raw, str(exc), judge=judge)

        raise MagiParseError(f"invalid structured response after one repair attempt: {last_error}")

    async def run_role(
        self,
        role: dict[str, str],
        target: dict[str, Any],
        query: str,
        timeout_seconds: int,
        event_callback: EventCallback | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = time.time()
        base = self._base_result(role, target)
        await self._emit(event_callback, {
            "type": "unit",
            "phase": "deliberating",
            "role": role["role"],
            "label": role["label"],
            "display": role.get("display"),
            "model": target.get("model"),
        })
        try:
            parsed, repaired = await self._structured_call(
                target,
                build_role_messages(query, role, evidence),
                parser=parse_magi_response,
                prompt_type="magi",
                timeout_seconds=timeout_seconds,
                max_tokens=1200,
                temperature=0.2,
            )
            result = {
                **base,
                "ok": True,
                "status": "answered",
                **parsed,
                "repaired": repaired,
                "latency_ms": self._elapsed_ms(started),
            }
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("MAGI role %s malfunctioned: %s", role["role"], exc)
            result = {
                **base,
                "ok": False,
                "status": "malfunction",
                "stance": "ERROR",
                "answer": "",
                "confidence": None,
                "reason": "",
                "risks": [],
                "dissent": "",
                "next_step": "",
                "error": str(exc)[:400],
                "latency_ms": self._elapsed_ms(started),
            }
        await self._emit(event_callback, {
            "type": "unit",
            "phase": result["status"],
            "role": role["role"],
            "unit": result,
        })
        return result

    async def run_debate_role(
        self,
        role: dict[str, str],
        target: dict[str, Any],
        query: str,
        first_round: list[dict[str, Any]],
        timeout_seconds: int,
        event_callback: EventCallback | None = None,
    ) -> dict[str, Any]:
        original = next((item for item in first_round if item.get("role") == role["role"]), None)
        if not original or not original.get("ok"):
            return original or {
                **self._base_result(role, target),
                "ok": False,
                "status": "malfunction",
                "stance": "ERROR",
                "answer": "",
                "error": "Initial round malfunctioned",
            }

        await self._emit(event_callback, {
            "type": "unit",
            "phase": "peer_review",
            "role": role["role"],
            "unit": original,
        })
        started = time.time()
        try:
            parsed, repaired = await self._structured_call(
                target,
                build_debate_messages(query, first_round, role),
                parser=parse_magi_response,
                prompt_type="magi-debate",
                timeout_seconds=timeout_seconds,
                max_tokens=1200,
                temperature=0.15,
            )
            result = {
                **original,
                **parsed,
                "status": "debated",
                "original_stance": original.get("stance"),
                "original_answer": original.get("answer") or "",
                "debated": True,
                "repaired": repaired,
                "latency_ms": self._elapsed_ms(started),
            }
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("MAGI debate role %s failed; retaining round one: %s", role["role"], exc)
            result = {
                **original,
                "status": "answered",
                "debated": False,
                "debate_status": "malfunction",
                "debate_error": str(exc)[:400],
            }
        await self._emit(event_callback, {
            "type": "unit",
            "phase": result["status"],
            "role": role["role"],
            "unit": result,
        })
        return result

    async def deliberate(
        self,
        query: str,
        targets: list[dict[str, Any]],
        *,
        mode: str = "vote",
        timeout_seconds: int = 180,
        judge_target: dict[str, Any] | None = None,
        event_callback: EventCallback | None = None,
        weighted: bool = False,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if mode not in {"vote", "debate", "judge"}:
            raise ValueError(f"Unsupported MAGI resolution mode: {mode}")
        active_targets = self._normalize_targets(targets)
        diversity = self._model_diversity(active_targets)
        if diversity["warning"]:
            self.logger.info("MAGI diversity: %s", diversity["warning"])
            await self._emit(event_callback, {
                "type": "system",
                "phase": "diversity_warning",
                "detail": diversity["warning"],
            })

        first_round = await asyncio.gather(*[
            self.run_role(role, target, query, timeout_seconds, event_callback, evidence)
            for role, target in zip(MAGI_ROLES, active_targets)
        ])
        magi = first_round

        if mode == "debate" and sum(1 for item in first_round if item.get("ok")) >= 2:
            await self._emit(event_callback, {"type": "system", "phase": "peer_review"})
            magi = await asyncio.gather(*[
                self.run_debate_role(role, target, query, first_round, timeout_seconds, event_callback)
                for role, target in zip(MAGI_ROLES, active_targets)
            ])

        vote = vote_resolution(magi, weighted=weighted)
        final = vote["final"]
        resolution = "debate_vote" if mode == "debate" else "vote"
        judge_error = None
        judge_result: dict[str, Any] | None = None
        answered_ok = sum(1 for item in magi if item.get("ok"))
        # Explicit judge mode runs on any valid unit; an auto-escalation from a
        # deadlock only makes sense when at least two units actually voted.
        should_judge = (mode == "judge" and answered_ok >= 1) or (
            bool(vote.get("requires_judge")) and answered_ok >= 2
        )

        if should_judge:
            selected_judge = judge_target or self._strongest_target(magi, active_targets)
            await self._emit(event_callback, {
                "type": "system",
                "phase": "judge",
                "model": selected_judge.get("model"),
            })
            try:
                judge_result, judge_repaired = await self._structured_call(
                    selected_judge,
                    build_judge_messages(query, magi),
                    parser=parse_judge_response,
                    prompt_type="magi-judge",
                    timeout_seconds=timeout_seconds,
                    max_tokens=1600,
                    temperature=0.1,
                    judge=True,
                )
                judge_result["model"] = selected_judge.get("model")
                judge_result["endpoint_id"] = selected_judge.get("endpoint_id")
                judge_result["repaired"] = judge_repaired
                final = (
                    f"{judge_result['verdict']}: {judge_result['final']}\n\n"
                    f"Agreement: {judge_result['agreement_summary']}\n"
                    f"Dissent: {judge_result['dissent_summary']}"
                )
                resolution = "judge" if mode == "judge" else "judge_escalated"
            except Exception as exc:  # noqa: BLE001
                judge_error = str(exc)[:400]
                resolution = "vote_fallback" if vote.get("winner") else "deadlock_unresolved"

        result = {
            "schema_version": 2,
            "id": str(uuid.uuid4()),
            "final": final,
            "decision": (judge_result or {}).get("verdict") or vote.get("winner"),
            "mode": mode,
            "resolution": resolution,
            "magi": magi,
            "agreement": vote["agreement"],
            "vote": {
                "winner": vote.get("winner"),
                "counts": vote.get("counts") or {},
                "weights": vote.get("weights") or {},
                "weighted": bool(weighted),
                "avg_confidence": vote.get("avg_confidence"),
                "dissent": vote.get("dissent") or [],
                "dissent_count": vote.get("dissent_count", 0),
                "answered_count": vote.get("answered_count", 0),
            },
            "evidence": {
                "url": evidence.get("url"),
                "title": evidence.get("title"),
                "chars": len(str(evidence.get("text") or "")),
            } if evidence else None,
            "model_diversity": diversity,
            "judge": judge_result,
            "degraded": any(not item.get("ok") for item in magi) or bool(judge_error),
            "malfunction_count": sum(1 for item in magi if not item.get("ok")),
            "judge_error": judge_error,
        }
        await self._emit(event_callback, {"type": "resolved", "phase": "resolved", "result": result})
        return result

    @staticmethod
    async def _default_call_model(*args, **kwargs) -> str:
        from src.llm_core import llm_call_async

        return await llm_call_async(*args, **kwargs)

    @staticmethod
    def _base_result(role: dict[str, str], target: dict[str, Any]) -> dict[str, Any]:
        return {
            "role": role["role"],
            "label": role["label"],
            "display": role.get("display") or role["label"],
            "title": role["title"],
            "model": target.get("model"),
            "endpoint_id": target.get("endpoint_id"),
            "endpoint_name": target.get("endpoint_name"),
        }

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return round((time.time() - started) * 1000)

    @staticmethod
    def _model_diversity(targets: list[dict[str, Any]]) -> dict[str, Any]:
        """Report how many genuinely distinct models back the units.

        Deliberation between copies of one model mostly re-samples the same
        distribution, so shared blind spots survive the vote. We never block
        on this (single-endpoint installs are legitimate) — we surface it.
        """
        unique = {(t.get("endpoint_id"), t.get("model")) for t in targets}
        warning = None
        if len(unique) < len(targets):
            models = ", ".join(sorted({str(t.get("model")) for t in targets}))
            warning = (
                f"only {len(unique)} distinct model(s) across {len(targets)} units ({models}); "
                "duplicated units share blind spots, weakening cross-validation"
            )
        return {
            "unique_models": len(unique),
            "total_units": len(targets),
            "warning": warning,
        }

    @staticmethod
    def _normalize_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        valid = [
            dict(target)
            for target in targets
            if target and target.get("endpoint") and target.get("model")
        ]
        if not valid:
            raise ValueError("MAGI requires at least one valid model target")
        out = list(valid)
        while len(out) < len(MAGI_ROLES):
            out.append(dict(valid[len(out) % len(valid)]))
        return out[: len(MAGI_ROLES)]

    @staticmethod
    def _strongest_target(
        magi: list[dict[str, Any]],
        targets: list[dict[str, Any]],
    ) -> dict[str, Any]:
        candidates = [
            (float(item.get("confidence") or 0), index)
            for index, item in enumerate(magi)
            if item.get("ok") and index < len(targets)
        ]
        if not candidates:
            return targets[0]
        _, index = max(candidates)
        return targets[index]
