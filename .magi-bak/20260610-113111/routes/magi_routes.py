"""MAGI tri-model deliberation routes."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

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



def setup_magi_routes(session_manager) -> APIRouter:
    router = APIRouter(prefix="/api/magi", tags=["magi"])
    orchestrator = MagiOrchestrator()

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
                "mode": "vote|judge|debate",
                "magi": [{"role": "string", "model": "string", "answer": "string", "stance": "string", "confidence": "number|null", "risks": ["string"], "dissent": "string", "next_step": "string"}],
                "agreement": "unanimous|majority|split",
            },
        }

    @router.post("/deliberate")
    async def deliberate(payload: MagiDeliberationRequest, request: Request):
        require_user(request)
        owner = effective_user(request) or ""
        defaults = _default_targets(owner)
        if not defaults:
            raise HTTPException(400, "No configured chat models are available for MAGI.")

        supplied_by_role = {(r.role or "").strip().lower(): r for r in payload.roles}
        targets: list[dict[str, Any]] = []
        for i, role in enumerate(MAGI_ROLES):
            selection = supplied_by_role.get(role["role"])
            if not selection and i < len(payload.roles):
                selection = payload.roles[i]
            target = _selection_target(selection, owner) if selection else None
            targets.append(target or defaults[i])

        result = await orchestrator.deliberate(
            payload.query,
            targets,
            mode=payload.mode,
            timeout_seconds=payload.timeout_seconds,
            judge_target=_selection_target(payload.judge, owner) if payload.judge else None,
        )

        if payload.session_id:
            _verify_session_owner(request, payload.session_id)
            try:
                sess = session_manager.get_session(payload.session_id)
                sess.add_message(ChatMessage("user", payload.display_query or payload.query))
                sess.add_message(ChatMessage("assistant", format_magi_markdown(result), metadata={
                    "group_model": "MAGI",
                    "magi": result,
                }))
                session_manager.save_sessions()
                try:
                    from core.database import update_session_last_accessed

                    update_session_last_accessed(payload.session_id)
                except Exception:
                    pass
            except KeyError as exc:
                raise HTTPException(404, f"Session '{payload.session_id}' not found") from exc

        return result

    return router
