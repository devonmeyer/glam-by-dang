#!/usr/bin/env python3
"""
Tests for the SQLite state store and the server paths that use it.
No network or API key needed.
Run: python3 tests/test_store.py   (or: pytest tests/test_store.py)
"""
import asyncio
import os
import sys
import tempfile

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from store import ConversationRecord, Store


class Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _store(path: str = ":memory:", clock=None) -> tuple[Store, int]:
    store = Store(path, clock=clock or Clock())
    return store, store.ensure_business("glam-by-dang", "Glam by Dang NYC")


def test_state_survives_reopen():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "mia.db")
        store, biz = _store(path)
        store.log_conversation(biz, ConversationRecord("123", "@ana", "Asked about brows", ["Answered FAQ"], False, 5.0))
        store.mark_owner_reply(biz, "123")
        store.record_bot_message(biz, "mid.1", ttl=3600)

        reopened, biz2 = _store(path)
        assert biz2 == biz
        [rec] = reopened.conversations(biz)
        assert rec.display_name == "@ana" and rec.actions_taken == ["Answered FAQ"]
        assert reopened.owner_replied_within(biz, "123", 3600)
        assert reopened.is_bot_message(biz, "mid.1")


def test_businesses_are_isolated():
    store, kha = _store()
    other = store.ensure_business("friend", "Friend Studio")
    store.log_conversation(kha, ConversationRecord("123", timestamp=1.0))
    store.mark_owner_reply(kha, "123")
    store.record_bot_message(kha, "mid.1", ttl=3600)
    assert store.conversations(other) == []
    assert not store.owner_replied_within(other, "123", 3600)
    assert not store.is_bot_message(other, "mid.1")


def test_latest_record_per_customer_wins():
    store, biz = _store()
    store.log_conversation(biz, ConversationRecord("123", conversation_summary="first", timestamp=1.0))
    store.log_conversation(biz, ConversationRecord("123", conversation_summary="second", timestamp=2.0))
    [rec] = store.conversations(biz)
    assert rec.conversation_summary == "second"


def test_clear_keeps_conversations_logged_after_the_summary():
    store, biz = _store()
    store.log_conversation(biz, ConversationRecord("1", timestamp=1.0))
    store.log_conversation(biz, ConversationRecord("2", timestamp=3.0))
    store.clear_conversations(biz, up_to=2.0)
    assert [r.sender_id for r in store.conversations(biz)] == ["2"]


def test_owner_takeover_expires():
    clock = Clock()
    store, biz = _store(clock=clock)
    store.mark_owner_reply(biz, "123")
    clock.now += 3599
    assert store.owner_replied_within(biz, "123", 3600)
    clock.now += 2
    assert not store.owner_replied_within(biz, "123", 3600)


def test_old_bot_messages_are_pruned():
    clock = Clock()
    store, biz = _store(clock=clock)
    store.record_bot_message(biz, "old", ttl=100)
    clock.now += 101
    store.record_bot_message(biz, "new", ttl=100)
    assert not store.is_bot_message(biz, "old")
    assert store.is_bot_message(biz, "new")


def test_summary_reads_from_store_and_clears_what_it_sent():
    import server

    server.store = Store(":memory:")
    server._business_id = None
    server.DAILY_SUMMARY_RECIPIENT = "owner"
    sent = []

    async def fake_send(account, recipient, text):
        sent.append((recipient, text))

    server._send_instagram_message = fake_send
    biz = server.business_id()
    server.store.log_conversation(biz, ConversationRecord("1", "@ana", "Wants a brow touch-up", ["Escalated to Kha"], True, 1.0))
    server.store.log_conversation(biz, ConversationRecord("2", "@bo", "Asked for prices", ["Answered FAQ"], False, 2.0))

    asyncio.run(server._send_daily_summary())

    text = "\n".join(t for _, t in sent)
    assert "@ana — Wants a brow touch-up" in text and "@bo — Asked for prices" in text
    assert server.store.conversations(biz) == []


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
