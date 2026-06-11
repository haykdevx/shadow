"""Workspace actions for the Shadow device agent.

These actions power the Desktop Workspace and Autonomous Missions: file tree,
content search, edits, multi-file patches, git, project commands, mission
checkpoints, and external-change detection. They run on the user's own
machine, dispatched through the authenticated outbound relay — the device
never opens an inbound port and is never exposed to the internet.

Containment is enforced HERE, independently of the server (defense in
depth): every path is resolved with ``os.path.realpath`` (so symlinks cannot
escape) and must stay inside BOTH the per-job workspace roots sent by the
server AND the device-local ``SHADOW_ALLOWED_ROOTS``. A compromised server
still cannot reach outside what the device owner configured locally.

Cross-platform: pure stdlib, ``pathlib`` + ``os.path.realpath`` +
``os.path.normcase`` (case-insensitive containment on Windows).
"""

from __future__ import annotations

import difflib
import hashlib
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

try:
    from companion.home_agent import HomeAgentError, _allowed_roots
except ImportError:  # Standalone installer places agent files side by side.
    from home_agent import HomeAgentError, _allowed_roots

WS_READ_ACTIONS = frozenset({
    "ws_tree",
    "ws_stat",
    "ws_read",
    "ws_search",
    "ws_hash",
    "ws_diff",
    "git_info",
    "git_diff",
    "git_log",
})
WS_WRITE_ACTIONS = frozenset({
    "ws_write",
    "ws_mkdir",
    "ws_rename",
    "ws_delete",
    "ws_patch",
    "ws_run",
    "git_commit",
    "git_checkout",
    "ws_checkpoint",
    "ws_restore",
})
WS_ALL_ACTIONS = WS_READ_ACTIONS | WS_WRITE_ACTIONS

MAX_READ_BYTES = 1_000_000
MAX_WRITE_BYTES = 2_000_000
MAX_RUN_SECONDS = 600
MAX_OUTPUT_CHARS = 200_000
TRASH_DIR = ".shadow-trash"
CHECKPOINT_DIR = ".shadow-checkpoints"
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
              TRASH_DIR, CHECKPOINT_DIR, ".tox", "dist", "build", ".next"}


def _is_windows() -> bool:
    return platform.system() == "Windows"


def _canon(path: str) -> str:
    """Realpath + normcase: the agent-side canonical form for containment."""
    return os.path.normcase(os.path.realpath(os.path.expanduser(str(path or ""))))


def _job_roots(args: dict[str, Any]) -> list[str]:
    """Intersect the server-supplied workspace roots with local allowed roots.

    The server names which workspace the job targets; the device owner's
    ``SHADOW_ALLOWED_ROOTS`` is the outer boundary that always applies.
    """
    requested = args.get("roots")
    requested = [str(r) for r in requested if str(r or "").strip()] if isinstance(requested, list) else []
    local = [_canon(str(r)) for r in _allowed_roots()]
    if not requested:
        raise HomeAgentError("Workspace jobs must name their authorized roots")
    roots: list[str] = []
    for raw in requested:
        canon = _canon(raw)
        if any(canon == lr or canon.startswith(lr.rstrip(os.sep) + os.sep) for lr in local):
            roots.append(canon)
        else:
            raise HomeAgentError("Workspace root is outside this device's SHADOW_ALLOWED_ROOTS")
    return roots


def _resolve(args: dict[str, Any], raw: str, *, must_exist: bool = False) -> Path:
    """Resolve a path against the job roots; reject every escape route."""
    roots = _job_roots(args)
    value = str(raw or "").strip()
    if "\x00" in value:
        raise HomeAgentError("Path contains a null byte")
    if not value:
        value = roots[0]
    if not os.path.isabs(os.path.expanduser(value)):
        value = os.path.join(roots[0], value)
    canon = _canon(value)
    if not any(canon == root or canon.startswith(root.rstrip(os.sep) + os.sep) for root in roots):
        raise HomeAgentError("Path is outside the authorized workspace")
    path = Path(canon)
    if must_exist and not path.exists():
        raise HomeAgentError(f"Path does not exist: {raw}")
    return path


def _rel(path: Path, args: dict[str, Any]) -> str:
    for root in _job_roots(args):
        canon = str(path)
        if canon == root:
            return "."
        if canon.startswith(root.rstrip(os.sep) + os.sep):
            return canon[len(root.rstrip(os.sep)) + 1:].replace(os.sep, "/")
    return str(path)


def _entry(path: Path) -> dict[str, Any]:
    try:
        st = path.stat()
    except OSError:
        return {"name": path.name, "type": "file", "error": "unreadable"}
    return {
        "name": path.name or str(path),
        "type": "dir" if path.is_dir() else "file",
        "size": st.st_size,
        "modified": int(st.st_mtime),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ── read actions ─────────────────────────────────────────────────────────


def _ws_tree(args: dict[str, Any]) -> dict[str, Any]:
    base = _resolve(args, str(args.get("path") or ""), must_exist=True)
    if not base.is_dir():
        raise HomeAgentError("Tree path is not a directory")
    depth = max(1, min(int(args.get("depth", 2)), 6))
    limit = max(1, min(int(args.get("limit", 500)), 2000))
    count = 0
    truncated = False

    def walk(directory: Path, level: int) -> list[dict[str, Any]]:
        nonlocal count, truncated
        rows: list[dict[str, Any]] = []
        try:
            children = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return rows
        for child in children:
            if child.name in _SKIP_DIRS or child.name.startswith(".git"):
                continue
            if count >= limit:
                truncated = True
                return rows
            count += 1
            row = _entry(child)
            row["path"] = _rel(child, args)
            if child.is_dir() and level < depth and not child.is_symlink():
                row["children"] = walk(child, level + 1)
            rows.append(row)
        return rows

    return {"ok": True, "path": _rel(base, args), "entries": walk(base, 1), "truncated": truncated}


def _ws_stat(args: dict[str, Any]) -> dict[str, Any]:
    path = _resolve(args, str(args.get("path") or ""), must_exist=True)
    row = _entry(path)
    row["path"] = _rel(path, args)
    if path.is_file():
        row["sha256"] = _sha256(path)
    return {"ok": True, **row}


def _ws_read(args: dict[str, Any]) -> dict[str, Any]:
    path = _resolve(args, str(args.get("path") or ""), must_exist=True)
    if not path.is_file():
        raise HomeAgentError("Path is not a file")
    max_bytes = max(1_000, min(int(args.get("max_bytes", MAX_READ_BYTES)), MAX_READ_BYTES))
    data = path.read_bytes()
    truncated = len(data) > max_bytes
    data = data[:max_bytes]
    binary = b"\x00" in data
    return {
        "ok": True,
        "path": _rel(path, args),
        "size": path.stat().st_size,
        "sha256": _sha256(path),
        "binary": binary,
        "truncated": truncated,
        "text": "" if binary else data.decode("utf-8", errors="replace"),
    }


def _ws_search(args: dict[str, Any]) -> dict[str, Any]:
    base = _resolve(args, str(args.get("path") or ""), must_exist=True)
    query = str(args.get("query") or "").strip()
    if not query:
        raise HomeAgentError("query is required")
    mode = str(args.get("mode") or "content")  # content | name
    limit = max(1, min(int(args.get("limit", 60)), 200))
    needle = query.lower()
    results: list[dict[str, Any]] = []
    visited_files = 0
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in files:
            child = Path(root) / name
            visited_files += 1
            if visited_files > 20000 or len(results) >= limit:
                return {"ok": True, "query": query, "mode": mode, "truncated": True, "results": results}
            if mode == "name":
                if needle in name.lower():
                    row = _entry(child)
                    row["path"] = _rel(child, args)
                    results.append(row)
                continue
            try:
                if child.stat().st_size > 2_000_000:
                    continue
                with child.open("rb") as handle:
                    blob = handle.read(2_000_000)
                if b"\x00" in blob[:4096]:
                    continue
                text = blob.decode("utf-8", errors="replace")
            except OSError:
                continue
            for line_no, line in enumerate(text.splitlines(), 1):
                if needle in line.lower():
                    results.append({
                        "path": _rel(child, args),
                        "line": line_no,
                        "text": line.strip()[:300],
                    })
                    if len(results) >= limit:
                        break
    return {"ok": True, "query": query, "mode": mode, "truncated": False, "results": results}


def _ws_hash(args: dict[str, Any]) -> dict[str, Any]:
    """Hashes+mtimes for external-change / conflict detection."""
    out = []
    for raw in (args.get("paths") or [])[:200]:
        try:
            path = _resolve(args, str(raw), must_exist=True)
            if path.is_file():
                out.append({"path": _rel(path, args), "sha256": _sha256(path),
                            "modified": int(path.stat().st_mtime)})
            else:
                out.append({"path": str(raw), "error": "not a file"})
        except HomeAgentError as exc:
            out.append({"path": str(raw), "error": str(exc)})
    return {"ok": True, "files": out}


def _ws_diff(args: dict[str, Any]) -> dict[str, Any]:
    """Unified diff between current file content and provided new text."""
    path = _resolve(args, str(args.get("path") or ""), must_exist=False)
    new_text = str(args.get("text") or "")
    old_text = ""
    if path.exists() and path.is_file():
        old_text = path.read_bytes()[:MAX_READ_BYTES].decode("utf-8", errors="replace")
    diff = "\n".join(difflib.unified_diff(
        old_text.splitlines(), new_text.splitlines(),
        fromfile=f"a/{_rel(path, args)}", tofile=f"b/{_rel(path, args)}", lineterm="",
    ))[:MAX_OUTPUT_CHARS]
    return {"ok": True, "path": _rel(path, args), "diff": diff, "exists": path.exists()}


# ── mutating actions ─────────────────────────────────────────────────────


def _checkpoint_root(args: dict[str, Any]) -> Path:
    return Path(_job_roots(args)[0]) / CHECKPOINT_DIR


def _snapshot_before_change(args: dict[str, Any], path: Path) -> None:
    """First-touch pre-image snapshot for the active checkpoint, if any."""
    checkpoint_id = str(args.get("checkpoint_id") or "").strip()
    if not checkpoint_id or not checkpoint_id.replace("-", "").replace("_", "").isalnum():
        return
    base = _checkpoint_root(args) / checkpoint_id
    rel = _rel(path, args)
    target = base / "files" / rel
    marker = base / "created" / rel
    if target.exists() or marker.exists():
        return  # only the first pre-image matters
    if path.exists() and path.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    else:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("created", encoding="utf-8")


def _ws_write(args: dict[str, Any]) -> dict[str, Any]:
    path = _resolve(args, str(args.get("path") or ""))
    text = str(args.get("text") or "")
    if len(text.encode("utf-8")) > MAX_WRITE_BYTES:
        raise HomeAgentError("Write is limited to 2 MB")
    expected = str(args.get("expect_sha256") or "").strip()
    if expected and path.exists() and path.is_file() and _sha256(path) != expected:
        raise HomeAgentError("Conflict: file changed on disk since it was read")
    _snapshot_before_change(args, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return {"ok": True, "path": _rel(path, args), "sha256": _sha256(path),
            "bytes": len(text.encode("utf-8"))}


def _ws_mkdir(args: dict[str, Any]) -> dict[str, Any]:
    path = _resolve(args, str(args.get("path") or ""))
    path.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "path": _rel(path, args)}


def _ws_rename(args: dict[str, Any]) -> dict[str, Any]:
    src = _resolve(args, str(args.get("path") or ""), must_exist=True)
    dst = _resolve(args, str(args.get("to") or ""))
    if dst.exists():
        raise HomeAgentError("Destination already exists")
    _snapshot_before_change(args, src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    return {"ok": True, "path": _rel(src, args), "to": _rel(dst, args)}


def _ws_delete(args: dict[str, Any]) -> dict[str, Any]:
    """Soft delete: move into the workspace trash so it is recoverable."""
    path = _resolve(args, str(args.get("path") or ""), must_exist=True)
    rel = _rel(path, args)
    if rel in (".", ""):
        raise HomeAgentError("Refusing to delete the workspace root")
    _snapshot_before_change(args, path)
    trash = Path(_job_roots(args)[0]) / TRASH_DIR / f"{int(time.time())}-{path.name}"
    trash.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(trash))
    return {"ok": True, "path": rel, "trash": _rel(trash, args)}


def _ws_patch(args: dict[str, Any]) -> dict[str, Any]:
    """Multi-file patch: list of edits, each exact old->new replacement or
    full-content write. All-or-nothing: validates everything first."""
    edits = args.get("edits")
    if not isinstance(edits, list) or not edits or len(edits) > 50:
        raise HomeAgentError("ws_patch needs 1-50 edits")
    plan: list[tuple[Path, str]] = []
    for edit in edits:
        if not isinstance(edit, dict):
            raise HomeAgentError("Each edit must be an object")
        path = _resolve(args, str(edit.get("path") or ""))
        if "old" in edit:
            if not path.is_file():
                raise HomeAgentError(f"Cannot patch missing file: {edit.get('path')}")
            current = path.read_bytes()[:MAX_READ_BYTES].decode("utf-8", errors="replace")
            old = str(edit.get("old") or "")
            new = str(edit.get("new") or "")
            if not old:
                raise HomeAgentError("Edit old-text must not be empty")
            occurrences = current.count(old)
            if occurrences == 0:
                raise HomeAgentError(f"Patch text not found in {_rel(path, args)}")
            if occurrences > 1 and not edit.get("replace_all"):
                raise HomeAgentError(f"Patch text is ambiguous ({occurrences}x) in {_rel(path, args)}")
            updated = current.replace(old, new) if edit.get("replace_all") else current.replace(old, new, 1)
        else:
            updated = str(edit.get("text") or "")
        if len(updated.encode("utf-8")) > MAX_WRITE_BYTES:
            raise HomeAgentError("Patched file exceeds 2 MB")
        plan.append((path, updated))
    changed = []
    for path, updated in plan:
        _snapshot_before_change(args, path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated, encoding="utf-8", newline="")
        changed.append({"path": _rel(path, args), "sha256": _sha256(path)})
    return {"ok": True, "changed": changed}


def _ws_run(args: dict[str, Any]) -> dict[str, Any]:
    command = str(args.get("command") or "").strip()
    if not command:
        raise HomeAgentError("command is required")
    cwd = _resolve(args, str(args.get("cwd") or ""), must_exist=True)
    if not cwd.is_dir():
        raise HomeAgentError("cwd is not a directory")
    timeout = max(1, min(int(args.get("timeout", 120)), MAX_RUN_SECONDS))
    argv = (["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
            if _is_windows() else ["/bin/bash", "-lc", command])
    started = time.time()
    try:
        proc = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True,
                              timeout=timeout, shell=False)
    except subprocess.TimeoutExpired as exc:
        raise HomeAgentError(f"Command timed out after {timeout}s") from exc
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[-MAX_OUTPUT_CHARS // 2:],
        "stderr": (proc.stderr or "")[-MAX_OUTPUT_CHARS // 2:],
        "seconds": round(time.time() - started, 2),
        "cwd": _rel(cwd, args),
    }


# ── git ──────────────────────────────────────────────────────────────────


def _git(args: dict[str, Any], git_args: list[str], *, timeout: int = 60) -> tuple[int, str, str]:
    cwd = _resolve(args, str(args.get("cwd") or ""), must_exist=True)
    try:
        proc = subprocess.run(["git", *git_args], cwd=str(cwd), capture_output=True,
                              text=True, timeout=timeout, shell=False)
    except FileNotFoundError as exc:
        raise HomeAgentError("git is not installed on this device") from exc
    except subprocess.TimeoutExpired as exc:
        raise HomeAgentError(f"git timed out after {timeout}s") from exc
    return proc.returncode, (proc.stdout or "")[:MAX_OUTPUT_CHARS], (proc.stderr or "")[:20000]


def _git_info(args: dict[str, Any]) -> dict[str, Any]:
    code, out, _ = _git(args, ["rev-parse", "--is-inside-work-tree"])
    if code != 0 or out.strip() != "true":
        return {"ok": True, "is_repo": False}
    _, branch, _ = _git(args, ["rev-parse", "--abbrev-ref", "HEAD"])
    _, head, _ = _git(args, ["rev-parse", "HEAD"])
    _, status, _ = _git(args, ["status", "--porcelain=v1"])
    _, branches, _ = _git(args, ["branch", "--format=%(refname:short)"])
    entries = [line for line in status.splitlines() if line.strip()]
    return {
        "ok": True,
        "is_repo": True,
        "branch": branch.strip(),
        "head": head.strip(),
        "dirty": bool(entries),
        "status": entries[:400],
        "branches": [b.strip() for b in branches.splitlines() if b.strip()][:100],
    }


def _git_log(args: dict[str, Any]) -> dict[str, Any]:
    limit = max(1, min(int(args.get("limit", 30)), 200))
    code, out, err = _git(args, ["log", f"-{limit}", "--format=%H%x1f%an%x1f%at%x1f%s"])
    if code != 0:
        raise HomeAgentError(err.strip() or "git log failed")
    commits = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            commits.append({"hash": parts[0], "author": parts[1],
                            "time": int(parts[2]), "subject": parts[3][:200]})
    return {"ok": True, "commits": commits}


def _git_diff(args: dict[str, Any]) -> dict[str, Any]:
    target = str(args.get("target") or "").strip()
    git_args = ["diff", "--no-color"]
    if target:
        if not all(ch.isalnum() or ch in "._-/~^" for ch in target):
            raise HomeAgentError("Invalid git diff target")
        git_args.append(target)
    if args.get("staged"):
        git_args.append("--cached")
    path = str(args.get("path") or "").strip()
    if path:
        git_args += ["--", str(_resolve(args, path))]
    code, out, err = _git(args, git_args)
    if code not in (0, 1):
        raise HomeAgentError(err.strip() or "git diff failed")
    _, stat, _ = _git(args, [a if a != "diff" else "diff" for a in git_args[:1]] + ["--stat"] + git_args[1:])
    return {"ok": True, "diff": out[:MAX_OUTPUT_CHARS], "stat": stat[:8000]}


def _git_commit(args: dict[str, Any]) -> dict[str, Any]:
    message = str(args.get("message") or "").strip()[:500]
    if not message:
        raise HomeAgentError("Commit message is required")
    code, _, err = _git(args, ["add", "-A"])
    if code != 0:
        raise HomeAgentError(err.strip() or "git add failed")
    code, out, err = _git(args, ["-c", "user.name=Shadow Mission",
                                 "-c", "user.email=mission@shadow.local",
                                 "commit", "-m", message])
    if code != 0:
        raise HomeAgentError((err or out).strip()[:500] or "git commit failed")
    _, head, _ = _git(args, ["rev-parse", "HEAD"])
    return {"ok": True, "head": head.strip(), "message": message}


def _git_checkout(args: dict[str, Any]) -> dict[str, Any]:
    branch = str(args.get("branch") or "").strip()
    if not branch or not all(ch.isalnum() or ch in "._-/" for ch in branch):
        raise HomeAgentError("Invalid branch name")
    git_args = ["checkout"]
    if args.get("create"):
        git_args.append("-b")
    git_args.append(branch)
    code, out, err = _git(args, git_args)
    if code != 0:
        raise HomeAgentError((err or out).strip()[:500] or "git checkout failed")
    return {"ok": True, "branch": branch}


# ── checkpoints / rollback ──────────────────────────────────────────────


def _ws_checkpoint(args: dict[str, Any]) -> dict[str, Any]:
    """Open a checkpoint: record git state (if a repo) and prepare the
    pre-image snapshot area used by mutating actions."""
    checkpoint_id = str(args.get("checkpoint_id") or "").strip()
    if not checkpoint_id or not checkpoint_id.replace("-", "").replace("_", "").isalnum():
        raise HomeAgentError("A valid checkpoint_id is required")
    base = _checkpoint_root(args) / checkpoint_id
    base.mkdir(parents=True, exist_ok=True)
    info: dict[str, Any] = {"created_at": time.time()}
    try:
        git = _git_info(args)
        if git.get("is_repo"):
            info["git"] = {"branch": git.get("branch"), "head": git.get("head"),
                           "dirty": git.get("dirty"), "status": git.get("status", [])[:100]}
    except HomeAgentError:
        pass
    (base / "meta.json").write_text(__import__("json").dumps(info, indent=2), encoding="utf-8")
    return {"ok": True, "checkpoint_id": checkpoint_id,
            "git": info.get("git"), "uncommitted_user_work": bool((info.get("git") or {}).get("dirty"))}


def _ws_restore(args: dict[str, Any]) -> dict[str, Any]:
    """Roll back files captured by a checkpoint.

    ``paths`` limits restore to specific files (per-file rollback); without
    it the whole checkpoint is restored: pre-images are copied back and
    files marked as created are removed (to trash).
    """
    checkpoint_id = str(args.get("checkpoint_id") or "").strip()
    if not checkpoint_id or not checkpoint_id.replace("-", "").replace("_", "").isalnum():
        raise HomeAgentError("A valid checkpoint_id is required")
    base = _checkpoint_root(args) / checkpoint_id
    if not base.exists():
        raise HomeAgentError("Checkpoint not found on this device")
    only = {str(p).replace("\\", "/") for p in (args.get("paths") or []) if str(p or "").strip()}
    root = Path(_job_roots(args)[0])
    restored: list[str] = []
    removed: list[str] = []

    files_dir = base / "files"
    if files_dir.exists():
        for snapshot in sorted(files_dir.rglob("*")):
            if not snapshot.is_file():
                continue
            rel = snapshot.relative_to(files_dir).as_posix()
            if only and rel not in only:
                continue
            target = _resolve(args, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(snapshot, target)
            restored.append(rel)

    created_dir = base / "created"
    if created_dir.exists():
        for marker in sorted(created_dir.rglob("*")):
            if not marker.is_file():
                continue
            rel = marker.relative_to(created_dir).as_posix()
            if only and rel not in only:
                continue
            target = root / rel
            if target.exists():
                trash = root / TRASH_DIR / f"rollback-{int(time.time())}" / rel
                trash.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(trash))
                removed.append(rel)
    return {"ok": True, "checkpoint_id": checkpoint_id,
            "restored": restored, "removed_created": removed}


# ── dispatch ─────────────────────────────────────────────────────────────

_HANDLERS = {
    "ws_tree": _ws_tree,
    "ws_stat": _ws_stat,
    "ws_read": _ws_read,
    "ws_search": _ws_search,
    "ws_hash": _ws_hash,
    "ws_diff": _ws_diff,
    "ws_write": _ws_write,
    "ws_mkdir": _ws_mkdir,
    "ws_rename": _ws_rename,
    "ws_delete": _ws_delete,
    "ws_patch": _ws_patch,
    "ws_run": _ws_run,
    "git_info": _git_info,
    "git_log": _git_log,
    "git_diff": _git_diff,
    "git_commit": _git_commit,
    "git_checkout": _git_checkout,
    "ws_checkpoint": _ws_checkpoint,
    "ws_restore": _ws_restore,
}


def execute_workspace_action(action: str, args: Any = None, *, confirmed: bool = False) -> dict[str, Any]:
    """Entry point called from ``home_agent.execute_action``.

    Mutating actions require the relay job to carry ``confirmed=True`` —
    the server's policy engine sets that only after an ALLOW decision or an
    explicit user approval, and read actions never need it.
    """
    args = args if isinstance(args, dict) else {}
    if action not in WS_ALL_ACTIONS:
        raise HomeAgentError(f"Unknown workspace action: {action}")
    if action in WS_WRITE_ACTIONS and not confirmed:
        raise HomeAgentError(f"Workspace action '{action}' requires confirmation")
    handler = _HANDLERS[action]
    return handler(args)
