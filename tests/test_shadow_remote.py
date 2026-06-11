import json

import pytest

from src import secret_storage, shadow_remote


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(shadow_remote, "REMOTE_STATE_PATH", tmp_path / "remote.json")
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    monkeypatch.setenv("SHADOW_MESH_ENABLED", "true")
    monkeypatch.setenv("SHADOW_MESH_GATEWAY_URL", "http://mesh-gateway:8099")
    monkeypatch.setenv("SHADOW_MESH_GATEWAY_KEY", "k" * 48)
    monkeypatch.setenv("SHADOW_MESH_PUBLIC_PATH", "/remote/")
    yield
    secret_storage._fernet = None


def test_remote_accounts_are_distinct_and_passwords_are_encrypted(monkeypatch):
    calls = []

    def fake_request(path, payload=None, **_kwargs):
        calls.append((path, payload))
        return {"ok": True}

    monkeypatch.setattr(shadow_remote, "_request", fake_request)
    alice = shadow_remote.ensure_account("alice")
    bob = shadow_remote.ensure_account("bob")

    assert alice["username"] != bob["username"]
    assert alice["group"] != bob["group"]
    assert alice["password"] != bob["password"]
    state = json.loads(shadow_remote.REMOTE_STATE_PATH.read_text(encoding="utf-8"))
    assert state["accounts"]["alice"]["password"].startswith("enc:")
    assert state["accounts"]["bob"]["password"].startswith("enc:")
    assert [path for path, _payload in calls] == ["/provision", "/provision"]


def test_invite_is_reduced_to_same_origin_remote_path(monkeypatch):
    def fake_request(path, payload=None, **_kwargs):
        if path == "/invite":
            return {
                "ok": True,
                "invite_url": "https://shadow.example/remote/agentinvite?c=secret",
            }
        return {"ok": True}

    monkeypatch.setattr(shadow_remote, "_request", fake_request)
    result = shadow_remote.create_invite("alice")
    assert result["invite_url"] == "/remote/agentinvite?c=secret"


def test_foreign_invite_path_is_rejected(monkeypatch):
    def fake_request(path, payload=None, **_kwargs):
        if path == "/invite":
            return {"ok": True, "invite_url": "https://evil.example/not-remote?c=secret"}
        return {"ok": True}

    monkeypatch.setattr(shadow_remote, "_request", fake_request)
    with pytest.raises(shadow_remote.ShadowRemoteError, match="invalid public URL"):
        shadow_remote.create_invite("alice")


def test_session_requires_short_lived_mesh_login_token(monkeypatch):
    def fake_request(path, payload=None, **_kwargs):
        if path == "/session":
            return {"ok": True, "token_user": "~t:abc123", "token_pass": "long-random-token"}
        return {"ok": True}

    monkeypatch.setattr(shadow_remote, "_request", fake_request)
    session = shadow_remote.create_session("alice")
    assert session["login_url"] == "/remote/login"
    assert session["token_user"].startswith("~t:")
    assert session["expires_in_seconds"] == 180


def test_remote_devices_are_sanitized(monkeypatch):
    def fake_request(path, payload=None, **_kwargs):
        if path == "/devices":
            return {
                "ok": True,
                "devices": [{
                    "_id": "node/remote/abc",
                    "name": "Alice PC",
                    "osdesc": "Windows 11",
                    "conn": 1,
                    "private": "must not leak",
                }],
            }
        return {"ok": True}

    monkeypatch.setattr(shadow_remote, "_request", fake_request)
    devices = shadow_remote.list_remote_devices("alice")
    assert devices == [{
        "id": "node/remote/abc",
        "name": "Alice PC",
        "os": "Windows 11",
        "connected": True,
    }]
