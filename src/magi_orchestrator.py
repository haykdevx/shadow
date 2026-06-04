"""MAGI deliberation orchestration.

Routes resolve auth and model targets. This module owns the execution graph:
fan-out, optional debate, vote resolution, optional judge synthesis, and
degraded-mode handling.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from src.magi_deliberation import (
    MAGI_ROLES,
    build_debate_messages,
    build_judge_messages,
    build_role_messages,
    parse_magi_response,
    vote_resolution,
)

MagiCall = Callable[..., Awaitable[str]]


class MagiOrchestrator:
    """Coordinate Shadow's three-role deliberation without route coupling."""

    def __init__(self, call_model: MagiCall | None = None, logger_: logging.Logger | None = None):
        self.call_model = call_model or self._default_call_model
        self.logger = logger_ or logging.getLogger(__name__)

    async def run_role(
        self,
        role: dict[str, str],
        target: dict[str, Any],
        query: str,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        started = time.time()
        base = self._base_result(role, target)
        try:
            raw = await self.call_model(
                target["endpoint"],
                target["model"],
                build_role_messages(query, role),
                headers=target.get("headers") or {},
                temperature=0.2,
                max_tokens=1200,
                timeout=timeout_seconds,
                max_retries=1,
                prompt_type="magi",
            )
            parsed = parse_magi_response(raw)
            return {
                **base,
                "ok": True,
                "status": "answered",
                "stance": parsed["stance"],
                "answer": parsed["answer"],
                "confidence": parsed.get("confidence"),
                "risks": parsed.get("risks") or [],
                "dissent": parsed.get("dissent") or "",
                "next_step": parsed.get("next_step") or "",
                "latency_ms": self._elapsed_ms(started),
            }
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("MAGI role %s failed: %s", role["role"], exc)
            return {
                **base,
                "ok": False,
                "status": "failed",
                "stance": "ERROR",
                "answer": "",
                "confidence": None,
                "risks": [],
                "dissent": "",
                "next_step": "",
                "error": str(exc)[:300],
                "latency_ms": self._elapsed_ms(started),
            }

    async def run_debate_role(
        self,
        role: dict[str, str],
        target: dict[str, Any],
        query: str,
        first_round: list[dict[str, Any]],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        original = next((item for item in first_round if item.get("role") == role["role"]), None)
        if not original or not original.get("ok"):
            return original or {
                **self._base_result(role, target),
                "ok": False,
                "status": "failed",
                "stance": "ERROR",
                "answer": "",
                "error": "Initial round failed",
            }

        started = time.time()
        try:
            raw = await self.call_model(
                target["endpoint"],
                target["model"],
                build_debate_messages(query, first_round, role),
                headers=target.get("headers") or {},
                temperature=0.15,
                max_tokens=1200,
                timeout=timeout_seconds,
                max_retries=1,
                prompt_type="magi-debate",
            )
            parsed = parse_magi_response(raw)
            return {
                **original,
                "status": "debated",
                "stance": parsed["stance"],
                "answer": parsed["answer"],
                "confidence": parsed.get("confidence"),
                "risks": parsed.get("risks") or [],
                "dissent": parsed.get("dissent") or "",
                "next_step": parsed.get("next_step") or "",
                "original_answer": original.get("answer") or "",
                "debated": True,
                "latency_ms": self._elapsed_ms(started),
            }
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("MAGI debate role %s failed: %s", role["role"], exc)
            return {
                **original,
                "status": "debate_failed",
                "debated": False,
                "debate_error": str(exc)[:300],
            }

    async def deliberate(
        self,
        query: str,
        targets: list[dict[str, Any]],
        *,
        mode: str = "vote",
        timeout_seconds: int = 180,
        judge_target: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run the full MAGI graph and return the public structured payload."""
        active_targets = self._normalize_targets(targets)
        magi = await asyncio.gather(*[
            self.run_role(role, target, query, timeout_seconds)
            for role, target in zip(MAGI_ROLES, active_targets)
        ])

        if mode == "debate" and sum(1 for item in magi if item.get("ok")) >= 2:
            magi = await asyncio.gather(*[
                self.run_debate_role(role, target, query, magi, timeout_seconds)
                for role, target in zip(MAGI_ROLES, active_targets)
            ])

        vote = vote_resolution(magi)
        final = vote["final"]
        resolution = "debate" if mode == "debate" else "vote"
        judge_error = None

        if mode == "judge" and any(m.get("ok") for m in magi):
            try:
                final = await self.call_model(
                    (judge_target or active_targets[0])["endpoint"],
                    (judge_target or active_targets[0])["model"],
                    build_judge_messages(query, magi),
                    headers=(judge_target or active_targets[0]).get("headers") or {},
                    temperature=0.15,
                    max_tokens=1600,
                    timeout=timeout_seconds,
                    max_retries=1,
                    prompt_type="magi-judge",
                )
                resolution = "judge"
            except Exception as exc:  # noqa: BLE001
                judge_error = str(exc)[:300]
                resolution = "vote_fallback"

        return {
            "id": str(uuid.uuid4()),
            "final": final,
            "mode": mode,
            "resolution": resolution,
            "magi": magi,
            "agreement": vote["agreement"],
            "vote": {
                "winner": vote["winner"],
                "counts": vote["counts"],
                "avg_confidence": vote.get("avg_confidence"),
                "dissent_count": vote.get("dissent_count", 0),
            },
            "degraded": any(not m.get("ok") for m in magi) or bool(judge_error),
            "judge_error": judge_error,
        }

    @staticmethod
    async def _default_call_model(*args, **kwargs) -> str:
        from src.llm_core import llm_call_async

        return await llm_call_async(*args, **kwargs)

    @staticmethod
    def _base_result(role: dict[str, str], target: dict[str, Any]) -> dict[str, Any]:
        return {
            "role": role["role"],
            "label": role["label"],
            "title": role["title"],
            "model": target.get("model"),
            "endpoint_id": target.get("endpoint_id"),
            "endpoint_name": target.get("endpoint_name"),
        }

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return round((time.time() - started) * 1000)

    @staticmethod
    def _normalize_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not targets:
            raise ValueError("MAGI requires at least one model target")
        out = list(targets)
        while len(out) < len(MAGI_ROLES):
            out.append(dict(out[len(out) % len(targets)]))
        return out[: len(MAGI_ROLES)]
