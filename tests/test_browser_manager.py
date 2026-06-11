"""Unit tests for the agent browser's policy layer (src/browser_manager.py).

These cover the pure/cheap pieces — risk gating, URL policy, pending
approvals, rate limiting, the task queue — without launching Chromium.
Live engine behavior is exercised separately in the e2e smoke pass.
"""

import asyncio
import json
import time

import pytest

from src import browser_manager, browser_tasks
from src.browser_manager import BrowserError, BrowserManager, classify_risk, check_url_allowed


# ── risk classifier ─────────────────────────────────────────────────────


@pytest.mark.parametrize("action,params,page,expect_gated", [
    ("read", {}, "https://shop.example/checkout", False),
    ("screenshot", {}, "https://bank.example", False),
    ("navigate", {"url": "https://amazon.com"}, "", False),
    ("click", {"text": "Buy now"}, "https://shop.example", True),
    ("click", {"text": "Place your order"}, "https://shop.example", True),
    ("click", {"text": "Delete account"}, "https://example.com", True),
    ("click", {"text": "Next page"}, "https://example.com", False),
    ("click", {"text": "Read more"}, "https://shop.example/checkout", True),
    ("press", {"key": "Enter"}, "https://shop.example/checkout/payment", True),
    ("press", {"key": "Tab"}, "https://shop.example/checkout", False),
    ("fill", {"selector": "#password", "value": "x"}, "https://example.com", True),
    ("fill", {"selector": "input[name=cvv]", "value": "123"}, "https://x.com", True),
    ("fill", {"selector": "#search", "value": "weather"}, "https://x.com", False),
    ("eval", {"js": "1+1"}, "https://example.com", True),
])
def test_classify_risk(action, params, page, expect_gated):
    gated, reason = classify_risk(action, params, page_url=page)
    assert gated is expect_gated, f"{action} {params} on {page}: {reason!r}"
    if gated:
        assert reason


def test_unknown_action_is_gated_not_allowed():
    gated, _ = classify_risk("teleport", {}, "")
    assert gated is True


# ── URL policy ──────────────────────────────────────────────────────────


def _allowed(url, monkeypatch=None, **env):
    async def go():
        return await check_url_allowed(url)
    if monkeypatch:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
    return asyncio.run(go())


def test_url_policy_blocks_non_http():
    ok, why = _allowed("file:///etc/passwd")
    assert not ok and "http" in why


def test_url_policy_blocks_loopback_and_private():
    for url in ("http://127.0.0.1/x", "http://localhost:8080", "http://10.0.0.5/admin",
                "http://169.254.169.254/latest/meta-data", "http://[::1]/", "http://foo.internal/"):
        ok, why = _allowed(url)
        assert not ok, url


def test_url_policy_allow_private_override(monkeypatch):
    ok, _ = _allowed("http://127.0.0.1:9000/x", monkeypatch, SHADOW_BROWSER_ALLOW_PRIVATE="1")
    assert ok


def test_url_policy_deny_list(monkeypatch):
    ok, why = _allowed("https://evil.example.com/page", monkeypatch, SHADOW_BROWSER_DENY_DOMAINS="evil.example.com")
    assert not ok and "deny list" in why


def test_url_policy_allow_list_excludes_everything_else(monkeypatch):
    monkeypatch.setenv("SHADOW_BROWSER_ALLOW_DOMAINS", "wikipedia.org")
    ok, _ = asyncio.run(check_url_allowed("https://en.wikipedia.org/wiki/Python"))
    assert ok
    ok, why = asyncio.run(check_url_allowed("https://example.com"))
    assert not ok and "allow list" in why


# ── pending approvals (no Chromium needed) ──────────────────────────────


def test_pending_approval_is_owner_scoped_and_redacted():
    manager = BrowserManager()
    shown = manager._add_pending(
        "alice", "fill", {"selector": "#password", "value": "hunter2"},
        "typing into a password/secret field", "agent:alice", "https://example.com/login?token=secret",
    )
    assert shown["display"]["value"] == "***"
    assert "hunter2" not in json.dumps(shown)
    assert "token=secret" not in json.dumps(shown)  # query stripped from page
    assert manager.list_pending("alice")[0]["id"] == shown["id"]
    assert manager.list_pending("bob") == []
    # bob cannot cancel alice's pending action
    with pytest.raises(BrowserError):
        manager.cancel("bob", shown["id"])
    assert manager.cancel("alice", shown["id"])["status"] == "cancelled"


def test_pending_approval_expires():
    manager = BrowserManager()
    shown = manager._add_pending("alice", "eval", {"js": "1"}, "r", "t", "https://x.com")
    manager._pending[shown["id"]]["expires_at"] = time.time() - 1
    assert manager.list_pending("alice") == []


def test_confirm_unknown_pending_raises():
    manager = BrowserManager()
    with pytest.raises(BrowserError):
        asyncio.run(manager.confirm("alice", "nope"))


# ── rate limiting ───────────────────────────────────────────────────────


def test_rate_limit_blocks_burst(monkeypatch):
    monkeypatch.setenv("SHADOW_BROWSER_ACTIONS_PER_MINUTE", "3")
    manager = BrowserManager()
    sess = browser_manager._OwnerSession("alice")
    for _ in range(3):
        manager._check_rate(sess)
    with pytest.raises(BrowserError, match="rate limit"):
        manager._check_rate(sess)


# ── owner slug / shot path safety ───────────────────────────────────────


def test_owner_slug_requires_real_owner():
    with pytest.raises(BrowserError):
        browser_manager._owner_slug("   ")


def test_shot_path_rejects_traversal(monkeypatch, tmp_path):
    monkeypatch.setattr(browser_manager, "DATA_ROOT", tmp_path)
    manager = BrowserManager()
    with pytest.raises(BrowserError):
        manager.shot_path("alice", "../../../etc/passwd")
    with pytest.raises(BrowserError):
        manager.shot_path("alice", "missing-id")


# ── file-backed browse-task queue ───────────────────────────────────────


def test_browse_task_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(browser_tasks, "TASKS_DIR", tmp_path)
    task_id = browser_tasks.enqueue("alice", "wikipedia python")
    assert browser_tasks.read_result(task_id) is None
    browser_tasks._write_result(task_id, {"ok": True, "text": "hi"})
    assert browser_tasks.read_result(task_id) == {"ok": True, "text": "hi"}


def test_browse_task_rejects_bad_ids(monkeypatch, tmp_path):
    monkeypatch.setattr(browser_tasks, "TASKS_DIR", tmp_path)
    with pytest.raises(ValueError):
        browser_tasks.read_result("../escape")


def test_confirm_refuses_when_page_context_changed():
    """An approval given on one page must not execute on another: with no
    live page the current context is blank, which differs from the cart
    page the gate was raised on."""
    manager = BrowserManager()
    shown = manager._add_pending(
        "alice", "click", {"text": "Buy"}, "risky", "web", "https://shop.example/cart"
    )
    with pytest.raises(BrowserError, match="page changed"):
        asyncio.run(manager.confirm("alice", shown["id"]))
    # Fail closed: the refused approval is consumed, not replayable.
    with pytest.raises(BrowserError, match="not found"):
        asyncio.run(manager.confirm("alice", shown["id"]))


def test_pending_display_excludes_full_page_url_and_params():
    manager = BrowserManager()
    shown = manager._add_pending(
        "alice", "fill", {"selector": "#p", "value": "hunter2"},
        "secret field", "web", "https://x.com/login?token=secret",
    )
    assert "params" not in shown and "page_url" not in shown
    listed = manager.list_pending("alice")[0]
    assert "params" not in listed and "page_url" not in listed
    assert "token=secret" not in str(listed)


def test_page_context_ignores_query_and_fragment():
    from src.browser_manager import _page_context

    assert _page_context("https://x.com/a?q=1#f") == _page_context("https://x.com/a?q=2")
    assert _page_context("https://x.com/a") != _page_context("https://x.com/b")
    assert _page_context("https://x.com/a") != _page_context("https://evil.com/a")
