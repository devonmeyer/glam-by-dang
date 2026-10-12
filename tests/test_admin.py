#!/usr/bin/env python3
"""
Tests for the admin panel API: login, creating and editing businesses, summaries,
connect links and logs. No network or API key needed.
Run: python3 tests/test_admin.py   (or: pytest tests/test_admin.py)
"""
import json
import logging
import os
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import business
import server
from store import ConversationRecord, Store

KHA_PROMPT = business.default_business().system_prompt
REAL_SEND_EMAIL = server.send_email


def _client() -> TestClient:
    os.environ.update(
        ADMIN_SECRET="admin-pw", INSTAGRAM_APP_ID="12345", META_APP_SECRET="s",
        PUBLIC_BASE_URL="https://itsmia.info", TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    )
    os.environ.pop("RESEND_API_KEY", None)
    server.store = Store(":memory:")
    server._business_id = None
    server.login_throttle = server.LoginThrottle()
    server.send_email = REAL_SEND_EMAIL
    business.load_business.cache_clear()
    return TestClient(server.app)


def _logged_in() -> TestClient:
    c = _client()
    assert c.post("/admin/api/login", json={"secret": "admin-pw"}).status_code == 200
    return c


def test_admin_secret_login_still_works_and_wrong_secret_is_refused():
    c = _client()
    assert c.get("/admin").status_code == 200
    assert c.get("/admin/api/businesses").status_code == 403
    assert c.post("/admin/api/login", json={"secret": "nope"}).status_code == 403
    assert c.get("/admin/api/session").json()["user"] is None
    assert c.post("/admin/api/login", json={"secret": "admin-pw"}).status_code == 200
    session = c.get("/admin/api/session").json()
    assert session["admin"] is True and session["user"]["email"] == "devon@itsmia.info"
    assert c.get("/admin/api/businesses").status_code == 200
    # The session also works for the curl-style endpoints
    assert c.get("/preview-summary").status_code == 200
    c.post("/admin/api/logout")
    assert c.get("/admin/api/businesses").status_code == 403


def test_forged_or_expired_session_is_refused():
    c = _logged_in()
    good = c.cookies.get(server.SESSION_COOKIE)
    uid, epoch, _, sig = good.split(".")
    c.cookies.set(server.SESSION_COOKIE, f"{uid}.{epoch}.9999999999.{sig}")
    assert c.get("/admin/api/businesses").status_code == 403
    user = server.User.from_row(server.store.user(int(uid)))
    expired = server.session_cookie("admin-pw", user, now=1000)
    c.cookies.set(server.SESSION_COOKIE, expired)
    assert c.get("/admin/api/businesses").status_code == 403
    other_key = server.session_cookie("other-secret", user)
    c.cookies.set(server.SESSION_COOKIE, other_key)
    assert c.get("/admin/api/businesses").status_code == 403


def test_list_shows_kha_from_the_repo():
    c = _logged_in()
    data = c.get("/admin/api/businesses").json()
    kha = next(b for b in data["businesses"] if b["slug"] == "glam-by-dang")
    assert kha["source"] == "repo" and kha["name"] == "Glam by Dang NYC"
    assert data["email_configured"] is False


def test_create_business_from_starter_and_edit_it():
    c = _logged_in()
    r = c.post("/admin/api/businesses", json={"slug": "lash-lab", "name": "Lash Lab", "owner_name": "Rosa"})
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "panel"
    assert c.post("/admin/api/businesses", json={"slug": "lash-lab", "name": "x", "owner_name": "y"}).status_code == 409
    assert c.post("/admin/api/businesses", json={"slug": "Bad Slug", "name": "x", "owner_name": "y"}).status_code == 400
    assert "lash-lab" in business.business_slugs()

    files = c.get("/admin/api/businesses/lash-lab").json()["files"]
    cfg = json.loads(files["business.json"])
    assert cfg["name"] == "Lash Lab" and "Rosa" in cfg["greeting"]
    cfg["summary_email"] = "rosa@example.com"
    files["business.json"] = json.dumps(cfg)
    files["context.md"] = "Lash lifts are $90.\n"
    r = c.put("/admin/api/businesses/lash-lab", json={"files": files})
    assert r.status_code == 200, r.text
    rosa = business.load_business("lash-lab")
    assert rosa.summary_email == "rosa@example.com"
    assert "Lash lifts are $90." in rosa.system_prompt and "Kha" not in rosa.system_prompt

    # Broken edits are refused and nothing changes
    bad = dict(files, **{"about.md": "Hi {{nope}}"})
    r = c.put("/admin/api/businesses/lash-lab", json={"files": bad})
    assert r.status_code == 400 and "nope" in r.json()["detail"]
    bad = dict(files, **{"business.json": "{"})
    assert c.put("/admin/api/businesses/lash-lab", json={"files": bad}).status_code == 400
    assert "Lash lifts are $90." in business.load_business("lash-lab").system_prompt


def test_copying_kha_replaces_her_name_in_canned_replies():
    c = _logged_in()
    r = c.post("/admin/api/businesses", json={"slug": "brow-bar", "name": "Brow Bar", "owner_name": "Ana",
                                              "copy_from": "glam-by-dang"})
    assert r.status_code == 200, r.text
    b = business.load_business("brow-bar")
    assert "Ana" in b.greeting and "Kha" not in b.escalation_reply
    assert b.summary_email == ""


def test_editing_kha_overrides_the_repo_until_reverted():
    c = _logged_in()
    files = c.get("/admin/api/businesses/glam-by-dang").json()["files"]
    files["context.md"] += "\nNew: brow lamination is $80.\n"
    assert c.put("/admin/api/businesses/glam-by-dang", json={"files": files}).status_code == 200
    assert "brow lamination is $80" in business.default_business().system_prompt
    assert business.business_source("glam-by-dang") == "panel"
    assert c.delete("/admin/api/businesses/glam-by-dang/saved").status_code == 200
    assert business.default_business().system_prompt == KHA_PROMPT

    # A panel-only business has no repo folder to go back to
    c.post("/admin/api/businesses", json={"slug": "lash-lab", "name": "Lash Lab", "owner_name": "Rosa"})
    assert c.delete("/admin/api/businesses/lash-lab/saved").status_code == 400


def test_summary_preview_and_send():
    c = _logged_in()
    c.post("/admin/api/businesses", json={"slug": "lash-lab", "name": "Lash Lab", "owner_name": "Rosa"})
    biz_id = server.store.ensure_business("lash-lab", "Lash Lab")
    server.store.log_conversation(biz_id, ConversationRecord("1", "Jo", "asked about lifts", timestamp=1.0))
    assert "asked about lifts" in c.get("/admin/api/businesses/lash-lab/summary").json()["text"]

    sent = []

    async def fake_send(slug=None):
        sent.append(slug)
    real = server._send_daily_summary
    server._send_daily_summary = fake_send
    try:
        assert c.post("/admin/api/businesses/lash-lab/summary").status_code == 200
    finally:
        server._send_daily_summary = real
    assert sent == ["lash-lab"]
    assert c.post("/admin/api/businesses/nope/summary").status_code == 404


def test_invite_link_and_email():
    c = _logged_in()
    r = c.post("/admin/api/businesses/glam-by-dang/invite", json={})
    assert r.status_code == 200 and r.json()["url"].startswith("https://itsmia.info/connect?invite=")
    assert "emailed" not in r.json()

    emails = []

    async def fake_email(to, subject, text, html=None):
        emails.append((to, text, html))
        return True
    real = server.send_email
    server.send_email = fake_email
    try:
        r = c.post("/admin/api/businesses/glam-by-dang/invite", json={"email": "kha@example.com"})
    finally:
        server.send_email = real
    assert r.json()["emailed"] is True
    assert emails[0][0] == "kha@example.com" and r.json()["url"] in emails[0][1]
    assert r.json()["url"].replace("&", "&amp;") in emails[0][2] and "Connect Instagram" in emails[0][2]
    # The emailed link works
    invite = r.json()["url"].split("invite=")[1]
    assert c.get(f"/connect?invite={invite}").status_code == 200


def test_logs_are_searchable():
    c = _logged_in()
    logging.getLogger("server").warning("token check failed for zebra-account")
    logging.getLogger("server").info("routine zebra note")
    lines = c.get("/admin/api/logs?q=zebra").json()["lines"]
    assert [l["message"] for l in lines][-2:] == ["token check failed for zebra-account", "routine zebra note"]
    warn = c.get("/admin/api/logs?q=zebra&level=WARNING").json()["lines"]
    assert [l["message"] for l in warn] == ["token check failed for zebra-account"]


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
