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


def test_agent_config_returns_group_id_and_same_origin_paths(monkeypatch):
    """The installer gets everything it needs to join unattended."""
    calls = []

    def fake_request(path, payload=None, **_kwargs):
        calls.append((path, payload))
        if path == "/groupid":
            return {"ok": True, "group_id": "AbC123$xyz@", "full_id": "mesh/remote/AbC123$xyz@"}
        return {"ok": True}

    monkeypatch.setattr(shadow_remote, "_request", fake_request)
    config = shadow_remote.agent_config("alice")

    assert config["group_id"] == "AbC123$xyz@"
    # Paths only — the installer joins them to the server it enrolled against,
    # so the result must never carry a hostname.
    assert config["public_path"] == "/remote/"
    assert config["agent_settings_path"] == "/remote/meshsettings"
    assert config["agent_script_path"] == "/remote/meshagents?script=1"
    assert config["agent_binary_path"] == "/remote/meshagents"
    for value in config.values():
        assert "://" not in str(value)
    # The account is provisioned first, so a brand-new device works immediately.
    assert [path for path, _payload in calls] == ["/provision", "/groupid"]


def test_agent_config_is_scoped_to_the_owner_group(monkeypatch):
    seen = []

    def fake_request(path, payload=None, **_kwargs):
        if path == "/groupid":
            seen.append(payload["group"])
            return {"ok": True, "group_id": "id-" + payload["group"]}
        return {"ok": True}

    monkeypatch.setattr(shadow_remote, "_request", fake_request)
    alice = shadow_remote.agent_config("alice")
    bob = shadow_remote.agent_config("bob")

    assert seen[0] != seen[1]
    assert alice["group_id"] != bob["group_id"]


def test_agent_config_rejects_a_missing_group_id(monkeypatch):
    def fake_request(path, payload=None, **_kwargs):
        if path == "/groupid":
            return {"ok": True, "group_id": ""}
        return {"ok": True}

    monkeypatch.setattr(shadow_remote, "_request", fake_request)
    with pytest.raises(shadow_remote.ShadowRemoteError):
        shadow_remote.agent_config("alice")
