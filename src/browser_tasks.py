"""File-backed browse-task queue between sidecar processes and the app.

The Telegram bridge runs in its own container and only shares `data/` with
the web app, so it cannot reach the live Playwright contexts directly.
Instead it drops a task file under `data/browser/tasks/` and polls for the
result file; the app process runs a small worker that executes queued tasks
with `src.browser_manager` under the requesting owner. No new network auth
surface, same pattern as the rest of the shadow-* shared-data bridges.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TASKS_DIR = Path(os.getenv("SHADOW_BROWSER_TASKS_DIR", "data/browser/tasks"))
TASK_TTL_SECONDS = 600
_ID_RE = re.compile(r"[A-Za-z0-9_-]{4,40}")


def _ensure_dir() -> None:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(TASKS_DIR, 0o700)


def _path(task_id: str, kind: str) -> Path:
    if not _ID_RE.fullmatch(str(task_id or "")):
        raise ValueError("Invalid browse task id")
    return TASKS_DIR / f"{task_id}.{kind}.json"


def enqueue(owner: str, instruction: str) -> str:
    """Sidecar side: queue a browse task for the app worker."""
    _ensure_dir()
    task_id = secrets.token_urlsafe(9)
    payload = {
        "id": task_id,
        "owner": str(owner or "").strip().lower(),
        "instruction": str(instruction or "").strip()[:2000],
        "created_at": time.time(),
    }
    tmp = _path(task_id, "task").with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.rename(_path(task_id, "task"))
    return task_id


def read_result(task_id: str) -> dict[str, Any] | None:
    """Sidecar side: fetch the result if the worker has finished."""
    path = _path(task_id, "result")
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def wait_result(task_id: str, timeout_seconds: float = 90, poll_seconds: float = 1.5) -> dict[str, Any] | None:
    """Sidecar side: block until the result lands or the timeout passes."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        result = read_result(task_id)
        if result is not None:
            return result
        time.sleep(poll_seconds)
    return None


def _write_result(task_id: str, payload: dict[str, Any]) -> None:
    tmp = _path(task_id, "result").with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.rename(_path(task_id, "result"))


def _purge_stale() -> None:
    cutoff = time.time() - TASK_TTL_SECONDS
    for path in TASKS_DIR.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            continue


async def worker_loop(poll_seconds: float = 2.0) -> None:
    """App side: execute queued tasks. Started from app startup."""
    from src.browser_manager import BrowserError, run_browse_task

    _ensure_dir()
    logger.info("Browser task worker started (dir=%s)", TASKS_DIR)
    while True:
        await asyncio.sleep(poll_seconds)
        try:
            _purge_stale()
            for path in sorted(TASKS_DIR.glob("*.task.json")):
                try:
                    task = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    path.unlink(missing_ok=True)
                    continue
                path.unlink(missing_ok=True)  # claim before running
                task_id = str(task.get("id") or "")
                owner = str(task.get("owner") or "")
                if not _ID_RE.fullmatch(task_id) or not owner:
                    continue
                logger.info("Browser task %s for owner=%s", task_id, owner)
                try:
                    # Hard cap per task so one stuck page can't stall the queue
                    # (the sidecar gives up at 90s; allow a little slack).
                    result = await asyncio.wait_for(
                        run_browse_task(owner, task.get("instruction") or ""),
                        timeout=110,
                    )
                    _write_result(task_id, {"ok": True, **result})
                except TimeoutError:
                    _write_result(task_id, {"ok": False, "error": "Browse task timed out"})
                except BrowserError as exc:
                    _write_result(task_id, {"ok": False, "error": str(exc)})
                except Exception as exc:  # noqa: BLE001 — worker must survive any task
                    logger.warning("Browser task %s crashed: %s", task_id, exc)
                    _write_result(task_id, {"ok": False, "error": f"Browse task failed: {str(exc)[:200]}"})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — keep the worker alive
            logger.warning("Browser task worker iteration failed: %s", exc)
