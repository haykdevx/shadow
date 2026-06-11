"""MAGI tri-model deliberation routes."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

from core.database import ModelEndpoint, SessionLocal
from core.models import ChatMessage
from routes.session_routes import _verify_session_owner
from src.auth_helpers import effective_user, owner_filter, require_user
from src.endpoint_resolver import (
    _endpoint_enabled_models,
    _endpoint_hidden_models,
    _first_chat_model,
    build_chat_url,
    build_headers,
    normalize_base,
)
from src.magi_deliberation import MAGI_ROLES, format_magi_markdown
from src.magi_orchestrator import MagiOrchestrator


class MagiModelSelection(BaseModel):
    role: str | None = Field(default=None, max_length=40)
    endpoint_id: str | None = Field(default=None, max_length=80)
    endpoint: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=300)


class MagiDeliberationRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=30000)
    display_query: str | None = Field(default=None, max_length=30000)
    session_id: str | None = Field(default=None, max_length=120)
    mode: Literal["vote", "judge", "debate"] = "vote"
    roles: list[MagiModelSelection] = Field(default_factory=list, max_length=3)
    judge: MagiModelSelection | None = None
    timeout_seconds: int = Field(default=180, ge=10, le=600)
    # Confidence-weighted voting: stances are ranked by summed unit
    # confidence instead of head count.
    weighted: bool = False
    # Evidence grounding: fetch one shared source (browser) before fan-out so
    # all units reason over the same retrieved text. Opt-in per query.
    evidence: bool = False
    evidence_query: str | None = Field(default=None, max_length=2000)


def _safe_json_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    if not raw:
        return []
    try:
        import json

        val = json.loads(raw)
        return [str(x) for x in val if str(x).strip()] if isinstance(val, list) else []
    except Exception:
        return []


def _is_llm_endpoint(ep: ModelEndpoint) -> bool:
    return (getattr(ep, "model_type", None) or "llm") == "llm"


def _visible_endpoints(owner: str) -> list[ModelEndpoint]:
    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
        if owner:
            q = owner_filter(q, ModelEndpoint, owner)
        rows = [ep for ep in q.order_by(ModelEndpoint.created_at.asc()).all() if _is_llm_endpoint(ep)]
        # Detach primitive fields before closing the DB session.
        out: list[ModelEndpoint] = []
        for ep in rows:
            clone = ModelEndpoint(
                id=ep.id,
                name=ep.name,
                base_url=ep.base_url,
                api_key=ep.api_key,
                is_enabled=ep.is_enabled,
                hidden_models=ep.hidden_models,
                cached_models=ep.cached_models,
                model_type=ep.model_type,
            )
            out.append(clone)
        return out
    finally:
        db.close()


def _target_from_endpoint(ep: ModelEndpoint, model: str | None = None) -> dict[str, Any] | None:
    base = normalize_base(ep.base_url)
    hidden = _endpoint_hidden_models(ep)
    enabled = _endpoint_enabled_models(ep)
    selected = (model or "").strip()
    if selected and selected in hidden:
        selected = ""
    if selected and enabled and selected not in enabled:
        selected = ""
    if not selected:
        selected = _first_chat_model(enabled) or ""
    if not selected:
        return None
    return {
        "endpoint_id": ep.id,
        "endpoint": build_chat_url(base),
        "endpoint_name": ep.name,
        "model": selected,
        "headers": build_headers(ep.api_key, base),
    }


def _selection_target(selection: MagiModelSelection | None, owner: str) -> dict[str, Any] | None:
    if not selection:
        return None
    endpoint_id = (selection.endpoint_id or "").strip()
    endpoint_url = (selection.endpoint or "").strip()
    model = (selection.model or "").strip()
    for ep in _visible_endpoints(owner):
        if endpoint_id and ep.id == endpoint_id:
            return _target_from_endpoint(ep, model)
        if endpoint_url:
            base = normalize_base(ep.base_url)
            variants = {base.rstrip("/"), build_chat_url(base).rstrip("/")}
            if endpoint_url.rstrip("/") in variants:
                return _target_from_endpoint(ep, model)
    return None


def _default_targets(owner: str) -> list[dict[str, Any]]:
    first_pass: list[dict[str, Any]] = []
    extras: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ep in _visible_endpoints(owner):
        enabled = _endpoint_enabled_models(ep)
        if not enabled:
            continue
        first = _first_chat_model(enabled)
        if first:
            target = _target_from_endpoint(ep, first)
            if target:
                key = f"{target['endpoint_id']}|{target['model']}"
                if key not in seen:
                    first_pass.append(target)
                    seen.add(key)
        for model in enabled:
            target = _target_from_endpoint(ep, model)
            if not target:
                continue
            key = f"{target['endpoint_id']}|{target['model']}"
            if key not in seen:
                extras.append(target)
                seen.add(key)
    targets = first_pass + extras
    if not targets:
        return []
    while len(targets) < 3:
        targets.append(dict(targets[len(targets) % len(first_pass or targets)]))
    return targets[:3]


def _available_models(owner: str) -> list[dict[str, Any]]:
    rows = []
    for ep in _visible_endpoints(owner):
        base = normalize_base(ep.base_url)
        rows.append({
            "endpoint_id": ep.id,
            "endpoint": build_chat_url(base),
            "endpoint_name": ep.name,
            "models": _endpoint_enabled_models(ep),
        })
    return rows



def _selection_is_explicit(selection: MagiModelSelection | None) -> bool:
    """A selection counts as user-chosen only when a concrete model is named."""
    return bool(selection and (selection.model or "").strip())


def _build_targets(payload: MagiDeliberationRequest, owner: str) -> list[dict[str, Any]]:
    """Resolve one target per MAGI role.

    Explicit (user-named) selections that resolve to a hidden or nonexistent
    model are rejected with 400 rather than silently falling back, so the user
    always knows which model actually ran. Empty selections use the auto
    defaults, which already maximize distinct endpoint/model assignments.
    """
    defaults = _default_targets(owner)
    if not defaults:
        raise HTTPException(400, "No configured chat models are available for MAGI.")

    supplied_by_role = {(r.role or "").strip().lower(): r for r in payload.roles}
    targets: list[dict[str, Any]] = []
    for i, role in enumerate(MAGI_ROLES):
        selection = supplied_by_role.get(role["role"])
        if selection is None and i < len(payload.roles):
            selection = payload.roles[i]
        if _selection_is_explicit(selection):
            target = _selection_target(selection, owner)
            if not target:
                raise HTTPException(
                    400,
                    f"Selected model for {role['label']} is unavailable or hidden.",
                )
            targets.append(target)
        else:
            targets.append(defaults[i])
    return targets


def _resolve_judge(payload: MagiDeliberationRequest, owner: str) -> dict[str, Any] | None:
    if not _selection_is_explicit(payload.judge):
        return None
    target = _selection_target(payload.judge, owner)
    if not target:
        raise HTTPException(400, "Selected MAGI judge model is unavailable or hidden.")
    return target


async def _gather_evidence(owner: str, payload: MagiDeliberationRequest) -> tuple[dict[str, Any] | None, str | None]:
    """Fetch one shared evidence source through the owner's agent browser.

    Failures degrade to evidence-free deliberation — a dead source must never
    abort the MAGI run. Returns (evidence, error_message).
    """
    if not payload.evidence:
        return None, None
    instruction = (payload.evidence_query or payload.query or "").strip()[:2000]
    try:
        from src.browser_manager import BrowserError, run_browse_task

        result = await asyncio.wait_for(run_browse_task(owner, instruction), timeout=60)
        return {"url": result.get("url"), "title": result.get("title"), "text": result.get("text")}, None
    except (BrowserError, asyncio.TimeoutError) as exc:
        logger.warning("MAGI evidence gathering failed: %s", exc)
        return None, str(exc)[:300]
    except Exception as exc:  # noqa: BLE001 — never let evidence kill the deliberation
        logger.warning("MAGI evidence gathering crashed: %s", exc)
        return None, f"evidence retrieval failed: {str(exc)[:200]}"


def setup_magi_routes(session_manager) -> APIRouter:
    router = APIRouter(prefix="/api/magi", tags=["magi"])
    orchestrator = MagiOrchestrator()

    def _save_magi_turn(session_id: str, display_query: str | None, query: str, result: dict[str, Any]) -> None:
        """Persist exactly one user message and one final MAGI assistant message."""
        try:
            sess = session_manager.get_session(session_id)
            sess.add_message(ChatMessage("user", display_query or query))
            sess.add_message(ChatMessage("assistant", format_magi_markdown(result), metadata={
                "group_model": "MAGI",
                "magi": result,
            }))
            session_manager.save_sessions()
            try:
                from core.database import update_session_last_accessed

                update_session_last_accessed(session_id)
            except Exception:
                pass
        except KeyError as exc:
            raise HTTPException(404, f"Session '{session_id}' not found") from exc

    @router.get("/config")
    def magi_config(request: Request):
        require_user(request)
        owner = effective_user(request) or ""
        defaults = _default_targets(owner)
        roles = []
        for i, role in enumerate(MAGI_ROLES):
            target = defaults[i] if i < len(defaults) else None
            roles.append({**role, "default": target})
        return {
            "roles": roles,
            "resolution_modes": ["vote", "judge", "debate"],
            "available": _available_models(owner),
            "payload_spec": {
                "final": "string",
                "decision": "APPROVE|REJECT|CONDITIONAL|null",
                "mode": "vote|judge|debate",
                "resolution": "vote|debate_vote|judge|judge_escalated|vote_fallback|deadlock_unresolved",
                "weighted": "boolean (confidence-weighted voting)",
                "evidence": "null | {url, title, chars} (shared retrieved evidence)",
                "model_diversity": "{unique_models, total_units, warning: string|null}",
                "agreement": "unanimous|majority|deadlock|insufficient|malfunction",
                "degraded": "boolean",
                "magi": [{
                    "role": "string", "label": "string", "model": "string",
                    "stance": "APPROVE|REJECT|CONDITIONAL|ERROR",
                    "answer": "string", "confidence": "number 0..1|null",
                    "reason": "string", "risks": ["string"], "dissent": "string",
                    "next_step": "string", "status": "answered|debated|malfunction",
                    "repaired": "boolean", "latency_ms": "number",
                }],
                "vote": {"winner": "string|null", "counts": {"STANCE": "number"}, "weights": {"STANCE": "number (summed confidence)"}, "weighted": "boolean", "avg_confidence": "number 0..1|null", "dissent_count": "number"},
                "judge": {"verdict": "string", "final": "string", "agreement_summary": "string", "dissent_summary": "string"},
            },
        }

    @router.post("/deliberate")
    async def deliberate(payload: MagiDeliberationRequest, request: Request):
        require_user(request)
        owner = effective_user(request) or ""
        targets = _build_targets(payload, owner)
        judge_target = _resolve_judge(payload, owner)
        if payload.session_id:
            _verify_session_owner(request, payload.session_id)

        evidence, evidence_error = await _gather_evidence(owner, payload)
        result = await orchestrator.deliberate(
            payload.query,
            targets,
            mode=payload.mode,
            timeout_seconds=payload.timeout_seconds,
            judge_target=judge_target,
            weighted=payload.weighted,
            evidence=evidence,
        )
        if evidence_error:
            result["evidence_error"] = evidence_error

        if payload.session_id:
            _save_magi_turn(payload.session_id, payload.display_query, payload.query, result)

        return result

    @router.post("/deliberate/stream")
    async def deliberate_stream(payload: MagiDeliberationRequest, request: Request):
        require_user(request)
        owner = effective_user(request) or ""
        # Resolve targets and session ownership up front so validation errors
        # surface as a normal HTTP status, not mid-stream.
        targets = _build_targets(payload, owner)
        judge_target = _resolve_judge(payload, owner)
        if payload.session_id:
            _verify_session_owner(request, payload.session_id)

        queue: asyncio.Queue = asyncio.Queue()

        async def on_event(event: dict[str, Any]) -> None:
            await queue.put(event)

        async def run() -> None:
            try:
                evidence = None
                if payload.evidence:
                    await queue.put({"type": "system", "phase": "evidence"})
                    evidence, evidence_error = await _gather_evidence(owner, payload)
                    await queue.put({
                        "type": "system",
                        "phase": "evidence_ready" if evidence else "evidence_failed",
                        "evidence": {"url": evidence.get("url"), "title": evidence.get("title")} if evidence else None,
                        "error": evidence_error,
                    })
                result = await orchestrator.deliberate(
                    payload.query,
                    targets,
                    mode=payload.mode,
                    timeout_seconds=payload.timeout_seconds,
                    judge_target=judge_target,
                    event_callback=on_event,
                    weighted=payload.weighted,
                    evidence=evidence,
                )
                if payload.session_id:
                    try:
                        _save_magi_turn(payload.session_id, payload.display_query, payload.query, result)
                    except Exception as exc:  # noqa: BLE001 - non-fatal; verdict already streamed
                        logger.warning("MAGI stream persistence failed: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("MAGI deliberation stream failed: %s", exc)
                await queue.put({"type": "error", "phase": "error", "error": str(exc)[:500]})
            finally:
                await queue.put(None)

        async def event_source():
            task = asyncio.create_task(run())
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=10.0)
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            break
                        yield ": keep-alive\n\n"
                        continue
                    if event is None:
                        break
                    yield f"data: {json.dumps(event, default=str)}\n\n"
                    if await request.is_disconnected():
                        break
            finally:
                # Client gone or stream finished: cancel any unfinished work.
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    return router
