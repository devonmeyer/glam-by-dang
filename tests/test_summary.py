#!/usr/bin/env python3
"""
Tests for per-business daily summaries (email, with Kha's Instagram DM fallback).
No network or API key needed.
Run: python3 tests/test_summary.py   (or: pytest tests/test_summary.py)
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import business
import server
from store import ConversationRecord, Store

emails: list[tuple[str, str, str]] = []
dms: list[tuple[str, str]] = []
email_ok = True


async def _fake_email(to, subject, text):
    emails.append((to, subject, text))
    return email_ok


async def _fake_dm(account, recipient, text):
    dms.append((recipient, text))


def _setup(tmp: Path, email_on: bool, kha_email: str = "", dm_recipient: str = "kha-ig"):
    """Two businesses: Kha's (copied from the repo) and a friend in Los Angeles."""
    global email_ok
    src = Path(__file__).parent.parent / "businesses" / "glam-by-dang"
    shutil.copytree(src, tmp / "glam-by-dang")
    kha = json.loads((tmp / "glam-by-dang" / "business.json").read_text())
    kha["summary_email"] = kha_email
    (tmp / "glam-by-dang" / "business.json").write_text(json.dumps(kha))
    shutil.copytree(src, tmp / "lash-lab")
    friend = dict(kha, name="Lash Lab", owner_name="Rosa", timezone="America/Los_Angeles",
                  summary_email="rosa@example.com", summary_hour=9)
    (tmp / "lash-lab" / "business.json").write_text(json.dumps(friend))

    business.BUSINESSES_DIR = tmp
    business.load_business.cache_clear()
    server.store = Store(":memory:")
    server._business_id = None
    server.send_email = _fake_email
    server.email_configured = lambda: email_on
    server._send_instagram_message = _fake_dm
    server.DAILY_SUMMARY_RECIPIENT = dm_recipient
    emails.clear()
    dms.clear()
    email_ok = True


def _log(slug: str, sender: str, summary: str, escalation: bool = False, ts: float = 1.0) -> int:
    b = business.load_business(slug)
    biz_id = server.store.ensure_business(b.slug, b.name)
    server.store.log_conversation(biz_id, ConversationRecord(sender, f"@{sender}", summary, [], escalation, ts))
    return biz_id


def _run(fn):
    original = business.BUSINESSES_DIR
    with tempfile.TemporaryDirectory() as d:
        try:
            fn(Path(d))
        finally:
            business.BUSINESSES_DIR = original
            business.load_business.cache_clear()


def test_each_owner_gets_only_their_own_conversations_by_email():
    def t(tmp):
        _setup(tmp, email_on=True, kha_email="kha@example.com")
        kha = _log("glam-by-dang", "ana", "Asked about brows")
        rosa = _log("lash-lab", "bo", "Wants a lash lift", escalation=True)

        asyncio.run(server._send_daily_summary("lash-lab"))
        [(to, subject, text)] = emails
        assert to == "rosa@example.com"
        assert "Lash Lab" in subject
        assert "@bo — Wants a lash lift" in text and "Needs Attention" in text
        assert "@ana" not in text
        assert server.store.conversations(rosa) == []
        assert len(server.store.conversations(kha)) == 1
        assert dms == []
    _run(t)


def test_kha_falls_back_to_instagram_dm_until_email_is_set_up():
    def t(tmp):
        _setup(tmp, email_on=False)
        kha = _log("glam-by-dang", "ana", "Asked about brows")
        asyncio.run(server._send_daily_summary())
        assert emails == []
        assert dms and dms[0][0] == "kha-ig" and "@ana — Asked about brows" in dms[0][1]
        assert server.store.conversations(kha) == []
    _run(t)


def test_failed_email_keeps_conversations_for_next_time():
    def t(tmp):
        global email_ok
        _setup(tmp, email_on=True)
        email_ok = False
        rosa = _log("lash-lab", "bo", "Wants a lash lift")
        asyncio.run(server._send_daily_summary("lash-lab"))
        assert len(emails) == 1
        assert len(server.store.conversations(rosa)) == 1
    _run(t)


def test_business_with_no_way_to_reach_owner_is_skipped():
    def t(tmp):
        _setup(tmp, email_on=False)
        rosa = _log("lash-lab", "bo", "Wants a lash lift")
        asyncio.run(server._send_daily_summary("lash-lab"))
        assert emails == [] and dms == []
        assert len(server.store.conversations(rosa)) == 1
    _run(t)


def test_summaries_go_out_at_each_owner_s_local_hour():
    def t(tmp):
        _setup(tmp, email_on=True)
        utc = ZoneInfo("UTC")
        # 10am New York (EDT) = 14:00 UTC; 9am Los Angeles (PDT) = 16:00 UTC
        assert [b.slug for b in server._due_for_summary(datetime(2026, 10, 13, 14, tzinfo=utc))] == ["glam-by-dang"]
        assert [b.slug for b in server._due_for_summary(datetime(2026, 10, 13, 16, tzinfo=utc))] == ["lash-lab"]
        assert server._due_for_summary(datetime(2026, 10, 13, 15, tzinfo=utc)) == []
    _run(t)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
