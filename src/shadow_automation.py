"""Small, explicit automation layer for Shadow Command.

Automations are intentionally conservative: they can request PC actions or
runbooks, but existing confirmation gates still control destructive execution.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from src.shadow_pc import ALL_ACTIONS, ShadowPcError, overview, request_action

AUTOMATIONS_PATH = Path(os.getenv("SHADOW_AUTOMATIONS_PATH", "data/shadow-automations.json"))
MAX_AUTOMATIONS = 64


def automation_templates() -> list[dict[str, Any]]:
    return [
        {
            "name": "Disk pressure report",
            "enabled": False,
            "trigger": {"type": "metric_threshold", "metric": "disk_used_percent", "op": ">", "value": 90},
            "action": {"type": "runbook", "name": "health_report"},
            "cooldown_seconds": 3600,
        },
        {
            "name": "High CPU snapshot",
            "enabled": False,
            "trigger": {"type": "metric_threshold", "metric": "cpu_percent", "op": ">", "value": 92},
            "action": {"type": "runbook", "name": "health_report"},
            "cooldown_seconds": 1800,
        },
        {
            "name": "Manual lock report",
            "enabled": True,
            "trigger": {"type": "manual"},
            "action": {"type": "runbook", "name": "lock_report"},
            "cooldown_seconds": 60,
        },
    ]


def list_automations() -> list[dict[str, Any]]:
    rows = _read_rows()
    return sorted(rows.values(), key=lambda row: (not row.get("enabled"), row.get("name", "")))


def save_automation(payload: dict[str, Any]) -> dict[str, Any]:
    rows = _read_rows()
    if len(rows) >= MAX_AUTOMATIONS and not payload.get("id"):
        raise ShadowPcError(f"Automation limit reached ({MAX_AUTOMATIONS})")
    item = _normalize_automation(payload, existing=rows.get(str(payload.get("id") or "")))
    rows[item["id"]] = item
    _write_rows(rows)
    return item


def delete_automation(automation_id: str) -> dict[str, Any]:
    rows = _read_rows()
    item = rows.pop(str(automation_id or ""), None)
    if not item:
        raise ShadowPcError("Automation not found")
    _write_rows(rows)
    return {"ok": True, "id": automation_id}


def evaluate_automations(*, automation_id: str | None = None, requested_by: str = "automation") -> dict[str, Any]:
    rows = _read_rows()
    snapshot = overview()
    now = time.time()
    events: list[dict[str, Any]] = []
    changed = False
    for item in rows.values():
        if automation_id and item["id"] != automation_id:
            continue
        if not item.get("enabled"):
            events.append({"id": item["id"], "name": item["name"], "status": "disabled"})
            continue
        if _cooling_down(item, now):
            events.append({"id": item["id"], "name": item["name"], "status": "cooldown"})
            continue
        triggered, detail = _triggered(item.get("trigger") or {}, snapshot)
        if not triggered:
            events.append({"id": item["id"], "name": item["name"], "status": "idle", "detail": detail})
            continue
        try:
            result = _request_automation_action(item, requested_by=requested_by)
            item["last_run_at"] = now
            item["last_status"] = result.get("status") or "requested"
            item["last_detail"] = detail
            item["updated_at"] = now
            changed = True
            events.append({"id": item["id"], "name": item["name"], "status": item["last_status"], "detail": detail, "result": result})
        except ShadowPcError as exc:
            item["last_status"] = "failed"
            item["last_detail"] = str(exc)
            item["updated_at"] = now
            changed = True
            events.append({"id": item["id"], "name": item["name"], "status": "failed", "detail": str(exc)})
    if changed:
        _write_rows(rows)
    return {"ok": True, "events": events, "snapshot": {"online": snapshot.get("online"), "demo": snapshot.get("demo")}}


def _request_automation_action(item: dict[str, Any], *, requested_by: str) -> dict[str, Any]:
    action = item.get("action") or {}
    action_type = str(action.get("type") or "").strip().lower()
    if action_type == "runbook":
        name = str(action.get("name") or "").strip()
        if not name:
            raise ShadowPcError("Automation runbook action needs a name")
        return request_action("runbook", {"name": name}, requested_by=f"{requested_by}:{item['name']}")
    if action_type == "pc_action":
        pc_action = str(action.get("action") or "").strip().lower()
        if pc_action not in ALL_ACTIONS:
            raise ShadowPcError(f"Unsupported automation PC action: {pc_action or '(missing)'}")
        return request_action(pc_action, action.get("args") or {}, requested_by=f"{requested_by}:{item['name']}")
    raise ShadowPcError("Automation action type must be runbook or pc_action")


def _triggered(trigger: dict[str, Any], snapshot: dict[str, Any]) -> tuple[bool, str]:
    trigger_type = str(trigger.get("type") or "manual").strip().lower()
    if trigger_type == "manual":
        return True, "manual trigger"
    if trigger_type != "metric_threshold":
        return False, f"unsupported trigger {trigger_type}"
    metric = str(trigger.get("metric") or "").strip()
    actual = _metric_value(snapshot, metric)
    if actual is None:
        return False, f"{metric or 'metric'} unavailable"
    try:
        expected = float(trigger.get("value"))
    except (TypeError, ValueError):
        return False, "invalid threshold"
    op = str(trigger.get("op") or ">").strip()
    passed = {
        ">": actual > expected,
        ">=": actual >= expected,
        "<": actual < expected,
        "<=": actual <= expected,
        "==": actual == expected,
    }.get(op, False)
    return passed, f"{metric}={round(actual, 2)} {op} {expected}"


def _metric_value(snapshot: dict[str, Any], metric: str) -> float | None:
    pc = snapshot.get("pc") or {}
    memory = pc.get("memory") or {}
    disk = pc.get("disk") or {}
    cpu = pc.get("cpu") or {}
    load = pc.get("load") or []
    gpu = (pc.get("gpu") or [{}])[0] if isinstance(pc.get("gpu"), list) else {}
    table = {
        "cpu_percent": cpu.get("percent"),
        "memory_percent": _ratio(memory.get("used"), memory.get("total")),
        "disk_used_percent": _ratio(disk.get("used"), disk.get("total")),
        "disk_free_gb": _gb(disk.get("free")),
        "load_1": load[0] if load else None,
        "gpu_temp_c": gpu.get("temp_c"),
        "gpu_percent": gpu.get("util_percent"),
    }
    value = table.get(metric)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ratio(used: Any, total: Any) -> float | None:
    try:
        used_f = float(used)
        total_f = float(total)
    except (TypeError, ValueError):
        return None
    if total_f <= 0:
        return None
    return (used_f / total_f) * 100


def _gb(value: Any) -> float | None:
    try:
        return float(value) / (1024 ** 3)
    except (TypeError, ValueError):
        return None


def _cooling_down(item: dict[str, Any], now: float) -> bool:
    try:
        cooldown = max(0, int(item.get("cooldown_seconds") or 0))
        last_run = float(item.get("last_run_at") or 0)
    except (TypeError, ValueError):
        return False
    return cooldown > 0 and last_run > 0 and now - last_run < cooldown


def _normalize_automation(payload: dict[str, Any], *, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ShadowPcError("Automation payload must be an object")
    now = time.time()
    item_id = str(payload.get("id") or (existing or {}).get("id") or uuid.uuid4()).strip()
    name = str(payload.get("name") or (existing or {}).get("name") or "").strip()[:100]
    if not name:
        raise ShadowPcError("Automation name is required")
    trigger = payload.get("trigger") or {}
    action = payload.get("action") or {}
    if not isinstance(trigger, dict) or not isinstance(action, dict):
        raise ShadowPcError("Automation trigger and action must be objects")
    _validate_action(action)
    if str(trigger.get("type") or "manual") not in {"manual", "metric_threshold"}:
        raise ShadowPcError("Automation trigger must be manual or metric_threshold")
    return {
        "id": item_id,
        "name": name,
        "enabled": bool(payload.get("enabled", (existing or {}).get("enabled", True))),
        "trigger": trigger,
        "action": action,
        "cooldown_seconds": max(0, min(int(payload.get("cooldown_seconds", (existing or {}).get("cooldown_seconds", 300)) or 0), 86400)),
        "last_run_at": (existing or {}).get("last_run_at"),
        "last_status": (existing or {}).get("last_status"),
        "last_detail": (existing or {}).get("last_detail"),
        "created_at": (existing or {}).get("created_at") or now,
        "updated_at": now,
    }


def _validate_action(action: dict[str, Any]) -> None:
    action_type = str(action.get("type") or "").strip().lower()
    if action_type == "runbook" and str(action.get("name") or "").strip():
        return
    if action_type == "pc_action" and str(action.get("action") or "").strip().lower() in ALL_ACTIONS:
        return
    raise ShadowPcError("Automation action must be a runbook name or supported PC action")


def _read_rows() -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(AUTOMATIONS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    rows = raw.get("automations") if isinstance(raw, dict) else raw
    if not isinstance(rows, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item_id, item in rows.items():
        try:
            normalized = _normalize_automation({**item, "id": item_id}, existing=item if isinstance(item, dict) else None)
            out[normalized["id"]] = normalized
        except Exception:
            continue
    return out


def _write_rows(rows: dict[str, dict[str, Any]]) -> None:
    AUTOMATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUTOMATIONS_PATH.write_text(json.dumps({"automations": rows}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
