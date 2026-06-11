"""Device-agent workspace actions: containment (traversal, absolute paths,
symlinks, server-supplied roots vs local allowed roots), patches, conflicts,
checkpoints/rollback in git and non-git folders, and command limits.

The agent functions are executed directly on temp directories — this is the
same code the installer ships to Windows/Linux/macOS devices.
"""

import os
import subprocess

import pytest

from companion.home_agent import HomeAgentError
from companion.workspace_agent import (
    WS_READ_ACTIONS,
    WS_WRITE_ACTIONS,
    execute_workspace_action as run,
)


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("SHADOW_ALLOWED_ROOTS", str(tmp_path))
    return {"root": root, "args": {"roots": [str(root)]}}


def w(ws, action, extra=None, confirmed=True):
    return run(action, {**ws["args"], **(extra or {})}, confirmed=confirmed)


# ── containment ──────────────────────────────────────────────────────────

def test_job_without_roots_is_rejected(ws):
    with pytest.raises(HomeAgentError, match="name their authorized roots"):
        run("ws_tree", {}, confirmed=False)


def test_server_root_outside_local_allowed_roots_rejected(ws, monkeypatch):
    monkeypatch.setenv("SHADOW_ALLOWED_ROOTS", str(ws["root"] / "sub-only"))
    (ws["root"] / "sub-only").mkdir()
    with pytest.raises(HomeAgentError, match="SHADOW_ALLOWED_ROOTS"):
        run("ws_tree", {"roots": [str(ws["root"])]}, confirmed=False)


@pytest.mark.parametrize("bad", [
    "../outside.txt",
    "a/../../outside.txt",
    "/etc/passwd",
    "~/.ssh/id_rsa",
])
def test_path_escapes_rejected(ws, bad):
    with pytest.raises(HomeAgentError):
        w(ws, "ws_read", {"path": bad})


def test_null_byte_rejected(ws):
    with pytest.raises(HomeAgentError, match="null byte"):
        w(ws, "ws_read", {"path": "a\x00b"})


def test_symlink_escape_rejected(ws, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    os.symlink(outside, ws["root"] / "link")
    with pytest.raises(HomeAgentError, match="outside the authorized workspace"):
        w(ws, "ws_read", {"path": "link/secret.txt"})


def test_mutations_require_confirmed_flag(ws):
    for action in sorted(WS_WRITE_ACTIONS):
        with pytest.raises(HomeAgentError, match="requires confirmation"):
            run(action, ws["args"], confirmed=False)


def test_reads_never_require_confirmation(ws):
    (ws["root"] / "a.txt").write_text("hello")
    for action in ("ws_tree", "ws_stat", "ws_read"):
        args = {"path": "a.txt"} if action != "ws_tree" else {}
        result = w(ws, action, args, confirmed=False)
        assert result["ok"]
    assert "ws_search" in WS_READ_ACTIONS


# ── file operations ──────────────────────────────────────────────────────

def test_write_read_roundtrip(ws):
    w(ws, "ws_write", {"path": "src/main.py", "text": "print('hi')\n"})
    result = w(ws, "ws_read", {"path": "src/main.py"}, confirmed=False)
    assert result["text"] == "print('hi')\n"
    assert result["sha256"]


def test_write_conflict_detection(ws):
    first = w(ws, "ws_write", {"path": "a.txt", "text": "v1"})
    (ws["root"] / "a.txt").write_text("changed externally")
    with pytest.raises(HomeAgentError, match="Conflict"):
        w(ws, "ws_write", {"path": "a.txt", "text": "v2", "expect_sha256": first["sha256"]})


def test_delete_is_soft_via_trash(ws):
    w(ws, "ws_write", {"path": "a.txt", "text": "x"})
    result = w(ws, "ws_delete", {"path": "a.txt"})
    assert not (ws["root"] / "a.txt").exists()
    assert (ws["root"] / result["trash"]).exists()


def test_delete_refuses_workspace_root(ws):
    with pytest.raises(HomeAgentError, match="workspace root"):
        w(ws, "ws_delete", {"path": "."})


def test_rename_within_workspace(ws):
    w(ws, "ws_write", {"path": "old.txt", "text": "x"})
    w(ws, "ws_rename", {"path": "old.txt", "to": "sub/new.txt"})
    assert (ws["root"] / "sub/new.txt").exists()


def test_rename_cannot_escape(ws):
    w(ws, "ws_write", {"path": "old.txt", "text": "x"})
    with pytest.raises(HomeAgentError):
        w(ws, "ws_rename", {"path": "old.txt", "to": "../escaped.txt"})


def test_search_content_and_names(ws):
    w(ws, "ws_write", {"path": "src/auth.py", "text": "def verify_password():\n    pass\n"})
    content = w(ws, "ws_search", {"query": "verify_password"}, confirmed=False)
    assert content["results"] and content["results"][0]["line"] == 1
    names = w(ws, "ws_search", {"query": "auth", "mode": "name"}, confirmed=False)
    assert any(r["path"] == "src/auth.py" for r in names["results"])


def test_multi_file_patch_all_or_nothing(ws):
    w(ws, "ws_write", {"path": "a.py", "text": "x = 1\n"})
    w(ws, "ws_write", {"path": "b.py", "text": "y = 2\n"})
    with pytest.raises(HomeAgentError, match="not found"):
        w(ws, "ws_patch", {"edits": [
            {"path": "a.py", "old": "x = 1", "new": "x = 10"},
            {"path": "b.py", "old": "MISSING", "new": "z"},
        ]})
    # nothing was applied
    assert (ws["root"] / "a.py").read_text() == "x = 1\n"
    result = w(ws, "ws_patch", {"edits": [
        {"path": "a.py", "old": "x = 1", "new": "x = 10"},
        {"path": "b.py", "old": "y = 2", "new": "y = 20"},
    ]})
    assert len(result["changed"]) == 2
    assert (ws["root"] / "b.py").read_text() == "y = 20\n"


def test_hash_for_external_change_detection(ws):
    w(ws, "ws_write", {"path": "a.txt", "text": "x"})
    first = w(ws, "ws_hash", {"paths": ["a.txt"]}, confirmed=False)["files"][0]["sha256"]
    (ws["root"] / "a.txt").write_text("y")
    second = w(ws, "ws_hash", {"paths": ["a.txt"]}, confirmed=False)["files"][0]["sha256"]
    assert first != second


# ── commands ─────────────────────────────────────────────────────────────

def test_run_command_with_output(ws):
    result = w(ws, "ws_run", {"command": "echo hello-$((20+3))"})
    assert result["returncode"] == 0 and "hello-23" in result["stdout"]


def test_run_command_timeout_enforced(ws):
    with pytest.raises(HomeAgentError, match="timed out"):
        w(ws, "ws_run", {"command": "sleep 5", "timeout": 1})


def test_run_cwd_must_be_inside_workspace(ws):
    with pytest.raises(HomeAgentError):
        w(ws, "ws_run", {"command": "ls", "cwd": "/etc"})


# ── checkpoints / rollback (non-git) ─────────────────────────────────────

def test_checkpoint_rollback_non_git(ws):
    w(ws, "ws_write", {"path": "keep.txt", "text": "original"})
    w(ws, "ws_checkpoint", {"checkpoint_id": "cp1"})
    w(ws, "ws_write", {"path": "keep.txt", "text": "mutated", "checkpoint_id": "cp1"})
    w(ws, "ws_write", {"path": "created.txt", "text": "new", "checkpoint_id": "cp1"})
    result = w(ws, "ws_restore", {"checkpoint_id": "cp1"})
    assert (ws["root"] / "keep.txt").read_text() == "original"
    assert not (ws["root"] / "created.txt").exists()
    assert "keep.txt" in result["restored"]
    assert "created.txt" in result["removed_created"]


def test_per_file_rollback(ws):
    w(ws, "ws_write", {"path": "a.txt", "text": "a1"})
    w(ws, "ws_write", {"path": "b.txt", "text": "b1"})
    w(ws, "ws_checkpoint", {"checkpoint_id": "cp2"})
    w(ws, "ws_write", {"path": "a.txt", "text": "a2", "checkpoint_id": "cp2"})
    w(ws, "ws_write", {"path": "b.txt", "text": "b2", "checkpoint_id": "cp2"})
    w(ws, "ws_restore", {"checkpoint_id": "cp2", "paths": ["a.txt"]})
    assert (ws["root"] / "a.txt").read_text() == "a1"
    assert (ws["root"] / "b.txt").read_text() == "b2"


def test_restore_unknown_checkpoint(ws):
    with pytest.raises(HomeAgentError, match="not found"):
        w(ws, "ws_restore", {"checkpoint_id": "nope1"})


# ── git (and rollback in a git repo) ─────────────────────────────────────

def _git_available():
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=5)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _git_available(), reason="git not installed")
def test_git_info_diff_commit_checkout(ws):
    subprocess.run(["git", "init", "-q", str(ws["root"])], check=True)
    w(ws, "ws_write", {"path": "a.txt", "text": "v1\n"})
    w(ws, "git_commit", {"message": "init"})
    info = w(ws, "git_info", {}, confirmed=False)
    assert info["is_repo"] and not info["dirty"]
    w(ws, "ws_write", {"path": "a.txt", "text": "v2\n"})
    diff = w(ws, "git_diff", {}, confirmed=False)
    assert "+v2" in diff["diff"]
    w(ws, "git_checkout", {"branch": "feature/x", "create": True})
    assert w(ws, "git_info", {}, confirmed=False)["branch"] == "feature/x"
    log = w(ws, "git_log", {}, confirmed=False)
    assert log["commits"][0]["subject"] == "init"


@pytest.mark.skipif(not _git_available(), reason="git not installed")
def test_checkpoint_rollback_inside_git_repo(ws):
    subprocess.run(["git", "init", "-q", str(ws["root"])], check=True)
    w(ws, "ws_write", {"path": "a.txt", "text": "committed\n"})
    w(ws, "git_commit", {"message": "base"})
    checkpoint = w(ws, "ws_checkpoint", {"checkpoint_id": "cpg"})
    assert checkpoint["git"]["branch"]
    assert checkpoint["uncommitted_user_work"] is False
    w(ws, "ws_write", {"path": "a.txt", "text": "mission edit\n", "checkpoint_id": "cpg"})
    w(ws, "ws_restore", {"checkpoint_id": "cpg"})
    assert (ws["root"] / "a.txt").read_text() == "committed\n"


@pytest.mark.skipif(not _git_available(), reason="git not installed")
def test_checkpoint_detects_uncommitted_user_work(ws):
    subprocess.run(["git", "init", "-q", str(ws["root"])], check=True)
    w(ws, "ws_write", {"path": "a.txt", "text": "x\n"})
    w(ws, "git_commit", {"message": "base"})
    w(ws, "ws_write", {"path": "a.txt", "text": "user wip\n"})  # dirty tree
    checkpoint = w(ws, "ws_checkpoint", {"checkpoint_id": "cpu"})
    assert checkpoint["uncommitted_user_work"] is True


def test_git_diff_target_injection_rejected(ws):
    with pytest.raises(HomeAgentError, match="Invalid git diff target"):
        w(ws, "git_diff", {"target": "HEAD; rm -rf /"}, confirmed=False)


def test_git_checkout_branch_name_validated(ws):
    with pytest.raises(HomeAgentError, match="Invalid branch"):
        w(ws, "git_checkout", {"branch": "x && evil"})
