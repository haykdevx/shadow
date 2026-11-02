"""SHADOW_ADMIN_USER / SHADOW_ADMIN_PASSWORD seed the first admin at app startup."""
import os

import pytest

from core.auth import AuthManager, bootstrap_admin_from_env


@pytest.fixture
def auth(tmp_path):
    return AuthManager(auth_path=str(tmp_path / "auth.json"))


def test_seeds_admin_when_password_set(auth, monkeypatch):
    monkeypatch.setenv("SHADOW_ADMIN_USER", " Owner ")
    monkeypatch.setenv("SHADOW_ADMIN_PASSWORD", "correct-horse-battery")
    assert bootstrap_admin_from_env(auth) == "created"
    assert auth.is_configured
    assert auth.verify_password("owner", "correct-horse-battery")
    assert auth.is_admin("owner")


def test_defaults_username_to_admin(auth, monkeypatch):
    monkeypatch.delenv("SHADOW_ADMIN_USER", raising=False)
    monkeypatch.setenv("SHADOW_ADMIN_PASSWORD", "correct-horse-battery")
    assert bootstrap_admin_from_env(auth) == "created"
    assert auth.verify_password("admin", "correct-horse-battery")


def test_noop_without_password(auth, monkeypatch):
    monkeypatch.delenv("SHADOW_ADMIN_PASSWORD", raising=False)
    assert bootstrap_admin_from_env(auth) == "no_password"
    assert not auth.is_configured


def test_never_overwrites_existing_accounts(auth, monkeypatch):
    assert auth.setup("existing", "existing-password-1")
    monkeypatch.setenv("SHADOW_ADMIN_USER", "intruder")
    monkeypatch.setenv("SHADOW_ADMIN_PASSWORD", "intruder-password-1")
    assert bootstrap_admin_from_env(auth) == "already_configured"
    assert not auth.verify_password("intruder", "intruder-password-1")


def test_rejects_short_password_and_reserved_names(auth, monkeypatch):
    monkeypatch.setenv("SHADOW_ADMIN_PASSWORD", "short")
    assert bootstrap_admin_from_env(auth) == "rejected"
    assert not auth.is_configured
    monkeypatch.setenv("SHADOW_ADMIN_USER", "system")
    monkeypatch.setenv("SHADOW_ADMIN_PASSWORD", "long-enough-password")
    assert bootstrap_admin_from_env(auth) == "rejected"
    assert not auth.is_configured
