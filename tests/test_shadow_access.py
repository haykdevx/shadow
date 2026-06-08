import pytest

from src import shadow_access


@pytest.fixture(autouse=True)
def _isolated_access(monkeypatch, tmp_path):
    monkeypatch.setattr(shadow_access, "ACCESS_PATH", tmp_path / "shadow-pc-access.json")
    monkeypatch.delenv("SHADOW_PC_OWNER", raising=False)


def test_telegram_pair_code_is_single_use_for_any_real_account():
    pair = shadow_access.create_telegram_pair_code("alice")
    linked = shadow_access.consume_telegram_pair_code(pair["code"], 12345, 98765)

    assert linked["username"] == "alice"
    assert shadow_access.telegram_identity(12345)["username"] == "alice"
    assert shadow_access.telegram_identity_for_chat(98765)["username"] == "alice"
    with pytest.raises(shadow_access.ShadowAccessError, match="invalid or expired"):
        shadow_access.consume_telegram_pair_code(pair["code"], 22222, 98765)


def test_pairing_one_identity_replaces_old_account_mapping():
    first = shadow_access.create_telegram_pair_code("alice")
    shadow_access.consume_telegram_pair_code(first["code"], 123, 456)
    second = shadow_access.create_telegram_pair_code("bob")
    shadow_access.consume_telegram_pair_code(second["code"], 123, 456)

    assert shadow_access.telegram_identity(123)["username"] == "bob"
    assert not shadow_access.access_summary("alice")["telegram_linked"]
    assert shadow_access.access_summary("bob")["telegram_linked"]


def test_pair_code_requires_real_account():
    with pytest.raises(shadow_access.ShadowAccessError, match="real Shadow account"):
        shadow_access.create_telegram_pair_code("")
