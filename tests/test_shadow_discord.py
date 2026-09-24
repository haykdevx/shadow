import pytest

from src import shadow_access
from src import shadow_discord_store as store


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(shadow_access, "ACCESS_PATH", tmp_path / "shadow-pc-access.json")
    monkeypatch.delenv("SHADOW_PC_OWNER", raising=False)
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "discord.json")


# ── Pairing (src.shadow_access) ──

def test_discord_pair_code_is_single_use_for_any_real_account():
    pair = shadow_access.create_discord_pair_code("alice")
    linked = shadow_access.consume_discord_pair_code(pair["code"], 12345, 98765)

    assert linked["username"] == "alice"
    assert shadow_access.discord_identity(12345)["username"] == "alice"
    assert shadow_access.discord_identity_for_chat(98765)["username"] == "alice"
    with pytest.raises(shadow_access.ShadowAccessError, match="invalid or expired"):
        shadow_access.consume_discord_pair_code(pair["code"], 22222, 98765)


def test_discord_pairing_one_identity_replaces_old_account_mapping():
    first = shadow_access.create_discord_pair_code("alice")
    shadow_access.consume_discord_pair_code(first["code"], 123, 456)
    second = shadow_access.create_discord_pair_code("bob")
    shadow_access.consume_discord_pair_code(second["code"], 123, 456)

    assert shadow_access.discord_identity(123)["username"] == "bob"
    assert not shadow_access.access_summary("alice")["discord_linked"]
    assert shadow_access.access_summary("bob")["discord_linked"]


def test_discord_pair_code_requires_real_account():
    with pytest.raises(shadow_access.ShadowAccessError, match="real Shadow account"):
        shadow_access.create_discord_pair_code("")


def test_discord_and_telegram_pairing_are_independent():
    # Same numeric id paired on both platforms must not collide — they live
    # in separate state keys / separate pair-code namespaces.
    tg_pair = shadow_access.create_telegram_pair_code("alice")
    dc_pair = shadow_access.create_discord_pair_code("alice")
    shadow_access.consume_telegram_pair_code(tg_pair["code"], 555, 111)
    shadow_access.consume_discord_pair_code(dc_pair["code"], 555, 222)

    assert shadow_access.telegram_identity(555)["username"] == "alice"
    assert shadow_access.discord_identity(555)["username"] == "alice"
    summary = shadow_access.access_summary("alice")
    assert summary["telegram_linked"] and summary["discord_linked"]

    shadow_access.unlink_discord("alice", 555)
    summary = shadow_access.access_summary("alice")
    assert summary["telegram_linked"] and not summary["discord_linked"]


def test_discord_pair_code_cannot_be_reused_as_telegram_code():
    dc_pair = shadow_access.create_discord_pair_code("alice")
    with pytest.raises(shadow_access.ShadowAccessError, match="invalid or expired"):
        shadow_access.consume_telegram_pair_code(dc_pair["code"], 1, 2)


def test_revoke_access_also_clears_discord_link():
    shadow_access.claim_owner("owner")
    pair = shadow_access.create_discord_pair_code("alice")
    shadow_access.consume_discord_pair_code(pair["code"], 9, 9)
    shadow_access.grant_access("owner", "alice", ["view"])

    shadow_access.revoke_access("owner", "alice")

    assert not shadow_access.access_summary("alice")["discord_linked"]


# ── Conversation history (src.shadow_discord_store) ──

def test_chat_history_is_scoped_to_shadow_owner():
    store.record_message("alice", 100, "in", "hello", discord_message_id=1, sender={"username": "Alice"})
    store.record_message("bob", 200, "in", "private", discord_message_id=2, sender={"username": "Bob"})

    assert [row["channel_id"] for row in store.list_chats("alice")] == [100]
    assert [row["channel_id"] for row in store.list_chats("bob")] == [200]
    assert store.messages("alice", 200) == []
    assert not store.owns_chat("alice", 200)


def test_message_dedup_and_read_state():
    store.record_message("alice", 100, "in", "hello", discord_message_id=1)
    store.record_message("alice", 100, "in", "hello", discord_message_id=1)
    assert len(store.messages("alice", 100)) == 1
    assert store.list_chats("alice")[0]["unread"] == 1
    assert store.mark_read("alice", 100)["ok"]
    assert store.list_chats("alice")[0]["unread"] == 0
