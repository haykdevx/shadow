import pytest

from src import shadow_access


@pytest.fixture(autouse=True)
def _isolated_access(monkeypatch, tmp_path):
    monkeypatch.setattr(shadow_access, "ACCESS_PATH", tmp_path / "shadow-pc-access.json")
    monkeypatch.setenv("SHADOW_PC_OWNER", "alice")


def test_owner_is_implicit_and_other_accounts_default_deny():
    assert shadow_access.permissions_for("alice") == {
        "view": True,
        "control": True,
        "approve": True,
        "owner": True,
    }
    assert shadow_access.permissions_for("bob")["view"] is False
    with pytest.raises(shadow_access.ShadowAccessError, match="permission"):
        shadow_access.require_permission("bob", "view")


def test_request_grant_and_revoke_access():
    request = shadow_access.request_access("bob", ["control"])
    assert request["permissions"] == {"approve": False, "control": True, "view": True}
    assert shadow_access.access_summary("alice")["requests"][0]["username"] == "bob"

    grant = shadow_access.grant_access("alice", "bob", ["approve"])
    assert grant["permissions"] == {"approve": True, "control": True, "view": True}
    assert shadow_access.require_permission("bob", "approve") == "bob"

    shadow_access.revoke_access("alice", "bob")
    assert shadow_access.permissions_for("bob")["view"] is False


def test_telegram_pair_code_is_single_use_and_inherits_permissions():
    shadow_access.grant_access("alice", "bob", ["view", "control"])
    pair = shadow_access.create_telegram_pair_code("bob")
    linked = shadow_access.consume_telegram_pair_code(pair["code"], 12345, 98765)

    assert linked["username"] == "bob"
    identity = shadow_access.telegram_identity(12345)
    assert identity["username"] == "bob"
    assert identity["permissions"]["control"] is True
    assert identity["permissions"]["approve"] is False

    with pytest.raises(shadow_access.ShadowAccessError, match="invalid or expired"):
        shadow_access.consume_telegram_pair_code(pair["code"], 22222, 98765)


def test_only_owner_can_grant_access():
    with pytest.raises(shadow_access.ShadowAccessError, match="Only the linked-PC owner"):
        shadow_access.grant_access("mallory", "bob", ["view"])
