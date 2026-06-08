import pytest

from src import shadow_telegram_store as store


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "telegram.json")


def test_chat_history_is_scoped_to_shadow_owner():
    store.record_message("alice", 100, "in", "hello", telegram_message_id=1, chat={"first_name": "Alice"})
    store.record_message("bob", 200, "in", "private", telegram_message_id=2, chat={"first_name": "Bob"})

    assert [row["chat_id"] for row in store.list_chats("alice")] == [100]
    assert [row["chat_id"] for row in store.list_chats("bob")] == [200]
    assert store.messages("alice", 200) == []
    assert not store.owns_chat("alice", 200)


def test_message_dedup_and_read_state():
    store.record_message("alice", 100, "in", "hello", telegram_message_id=1)
    store.record_message("alice", 100, "in", "hello", telegram_message_id=1)
    assert len(store.messages("alice", 100)) == 1
    assert store.list_chats("alice")[0]["unread"] == 1
    assert store.mark_read("alice", 100)["ok"]
    assert store.list_chats("alice")[0]["unread"] == 0
