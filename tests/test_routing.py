#!/usr/bin/env python3
"""
Tests for routing Instagram webhooks to the business that owns the account.
No network or API key needed.
Run: python3 tests/test_routing.py   (or: pytest tests/test_routing.py)
"""
import hashlib
import hmac
import json
import os
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi.testclient import TestClient

import server
from store import Store

SECRET = "test-app-secret"
KHA_ACCOUNT = "17841400000000001"
OTHER_ACCOUNT = "17841400000000002"
client = TestClient(server.app)
handled: list[tuple[str, str, str]] = []  # (account id, business slug, sender)


async def _record_dm(account, sender_id, text):
    handled.append((account.account_id, account.business.slug, sender_id))


def _reset():
    server.store = Store(":memory:")
    server._business_id = None
    server.META_APP_SECRET = SECRET
    server._handle_instagram_dm = _record_dm
    server._connected_tokens.clear()
    handled.clear()


def _post(entries: list[dict]):
    body = json.dumps({"object": "instagram", "entry": entries}).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    r = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sig})
    assert r.status_code == 200, r.text


def _dm(account_id: str, sender: str = "555", text: str = "hi") -> dict:
    return {"id": account_id, "messaging": [
        {"sender": {"id": sender}, "recipient": {"id": account_id}, "message": {"mid": "m1", "text": text}}
    ]}


def _echo(account_id: str, customer: str = "555", mid: str = "owner-mid") -> dict:
    return {"id": account_id, "messaging": [
        {"sender": {"id": account_id}, "recipient": {"id": customer},
         "message": {"mid": mid, "text": "hey!", "is_echo": True}}
    ]}


def test_first_webhook_registers_kha_account():
    _reset()
    _post([_dm(KHA_ACCOUNT)])
    assert handled == [(KHA_ACCOUNT, "glam-by-dang", "555")]
    assert server.store.accounts_for_business(server.business_id()) == [KHA_ACCOUNT]
    assert server.default_account().account_id == KHA_ACCOUNT


def test_unknown_account_ignored_once_there_are_two_businesses():
    _reset()
    _post([_dm(KHA_ACCOUNT)])
    server.store.ensure_business("friend", "Friend Studio")
    handled.clear()
    _post([_dm(OTHER_ACCOUNT)])
    assert handled == []
    _post([_dm(KHA_ACCOUNT)])
    assert handled == [(KHA_ACCOUNT, "glam-by-dang", "555")]


def test_business_without_a_token_is_ignored():
    _reset()
    _post([_dm(KHA_ACCOUNT)])
    friend = server.store.ensure_business("friend", "Friend Studio")
    server.store.register_account(OTHER_ACCOUNT, friend)
    handled.clear()
    _post([_dm(OTHER_ACCOUNT)])
    assert handled == []


def test_owner_takeover_is_scoped_to_the_account_it_happened_on():
    _reset()
    _post([_dm(KHA_ACCOUNT)])
    friend = server.store.ensure_business("friend", "Friend Studio")
    server.store.register_account(OTHER_ACCOUNT, friend)
    original = server._tokens_for
    server._tokens_for = lambda account_id, slug: server.token_store
    server.load_business = lambda slug: server.default_business()  # no friend folder in the repo
    try:
        _post([_echo(OTHER_ACCOUNT)])
    finally:
        server._tokens_for = original
        server.load_business = __import__("business").load_business
    assert server.store.owner_replied_within(friend, "555", 3600)
    assert not server.store.owner_replied_within(server.business_id(), "555", 3600)


def test_mia_echo_is_not_an_owner_reply():
    _reset()
    _post([_dm(KHA_ACCOUNT)])
    server.store.record_bot_message(server.business_id(), "mia-mid", ttl=3600)
    _post([_echo(KHA_ACCOUNT, mid="mia-mid")])
    assert not server.store.owner_replied_within(server.business_id(), "555", 3600)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
