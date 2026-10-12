#!/usr/bin/env python3
"""
Tests for connecting a business's Instagram through Business Login for Instagram.
Instagram's API is stubbed; no network or API key needed.
Run: python3 tests/test_connect.py   (or: pytest tests/test_connect.py)
"""
import base64
import hashlib
import hmac
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import business
import instagram_connect
import server
from store import ConversationRecord, Store

SECRET = "test-app-secret"
FRIEND_ACCOUNT = "17841400000000002"
client = TestClient(server.app)
_tmp = tempfile.TemporaryDirectory()


def _reset():
    os.environ.update(
        ADMIN_SECRET="admin-pw", INSTAGRAM_APP_ID="12345", META_APP_SECRET=SECRET,
        PUBLIC_BASE_URL="https://itsmia.info", TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    )
    server.META_APP_SECRET = SECRET
    server.store = Store(":memory:")
    server._business_id = None
    server._connected_tokens.clear()

    # A second business folder next to Kha's
    tmp = Path(_tmp.name)
    shutil.rmtree(tmp, ignore_errors=True)
    src = Path(__file__).parent.parent / "businesses" / "glam-by-dang"
    shutil.copytree(src, tmp / "glam-by-dang")
    shutil.copytree(src, tmp / "lash-lab")
    cfg = json.loads((tmp / "lash-lab" / "business.json").read_text())
    cfg.update(name="Lash Lab", owner_name="Rosa")
    (tmp / "lash-lab" / "business.json").write_text(json.dumps(cfg))
    business.BUSINESSES_DIR = tmp
    business.load_business.cache_clear()

    async def exchange_code(http, code):
        assert code == "the-code"
        return "short-token"

    async def long_lived(http, short):
        assert short == "short-token"
        return "long-token", 60 * 24 * 3600

    async def whoami(http, token):
        return {"user_id": FRIEND_ACCOUNT, "id": "app-scoped-9", "username": "lashlab"}

    async def subscribe(http, token):
        assert token == "long-token"

    instagram_connect.exchange_code = exchange_code
    instagram_connect.long_lived = long_lived
    instagram_connect.whoami = whoami
    instagram_connect.subscribe = subscribe


def _invite(slug="lash-lab") -> str:
    r = client.post("/admin/invites", json={"slug": slug}, headers={"X-Admin-Secret": "admin-pw"})
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    assert url.startswith("https://itsmia.info/connect?invite=")
    return parse_qs(urlparse(url).query)["invite"][0]


def _signed(payload: dict, secret: str = SECRET) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    return f"{sig}.{body}"


def test_invites_need_the_admin_secret_and_a_real_business():
    _reset()
    assert client.post("/admin/invites", json={"slug": "lash-lab"}).status_code == 403
    assert client.post("/admin/invites", json={"slug": "lash-lab"},
                       headers={"X-Admin-Secret": "wrong"}).status_code == 403
    assert client.post("/admin/invites", json={"slug": "nope"},
                       headers={"X-Admin-Secret": "admin-pw"}).status_code == 404


def test_connect_page_links_to_instagram_with_the_invite_as_state():
    _reset()
    invite = _invite()
    r = client.get(f"/connect?invite={invite}")
    assert r.status_code == 200 and "Lash Lab" in r.text
    assert "https://www.instagram.com/oauth/authorize?" in r.text
    assert f"state={invite}" in r.text and "client_id=12345" in r.text
    assert client.get("/connect?invite=made-up").status_code == 404


def test_callback_connects_the_account_to_its_business():
    _reset()
    invite = _invite()
    r = client.get(f"/auth/instagram/callback?code=the-code%23_&state={invite}")
    assert r.status_code == 200 and "@lashlab" in r.text, r.text

    biz_id, slug = server.store.account_business(FRIEND_ACCOUNT)
    assert slug == "lash-lab"
    token_enc, expires_at, _ = server.store.load_token(FRIEND_ACCOUNT)
    assert "long-token" not in token_enc  # stored encrypted
    assert expires_at is not None

    # The invite is single-use
    assert client.get(f"/auth/instagram/callback?code=the-code&state={invite}").status_code == 404

    # Webhooks for that account now resolve to Lash Lab with its own token
    account = server._account_for(FRIEND_ACCOUNT)
    assert account.business.name == "Lash Lab" and account.token == "long-token"


def test_token_survives_restart_and_refresh_stays_encrypted():
    _reset()
    client.get(f"/auth/instagram/callback?code=the-code&state={_invite()}")
    server._connected_tokens.clear()  # as after a redeploy
    tokens = server._tokens_for(FRIEND_ACCOUNT, "lash-lab")
    assert tokens.token == "long-token"
    tokens.apply_refresh("refreshed-token", 60 * 24 * 3600)
    server._connected_tokens.clear()
    assert server._tokens_for(FRIEND_ACCOUNT, "lash-lab").token == "refreshed-token"
    assert "refreshed-token" not in server.store.load_token(FRIEND_ACCOUNT)[0]


def test_declined_login_leaves_the_invite_usable():
    _reset()
    invite = _invite()
    r = client.get(f"/auth/instagram/callback?state={invite}&error=access_denied&error_description=User+denied")
    assert r.status_code == 400 and "User denied" in r.text
    assert client.get(f"/connect?invite={invite}").status_code == 200


def test_deauthorize_disconnects_the_account():
    _reset()
    client.get(f"/auth/instagram/callback?code=the-code&state={_invite()}")
    assert client.post("/auth/instagram/deauthorize",
                       data={"signed_request": _signed({"user_id": "app-scoped-9"}, "wrong")}).status_code == 403
    r = client.post("/auth/instagram/deauthorize", data={"signed_request": _signed({"user_id": "app-scoped-9"})})
    assert r.status_code == 200
    assert server.store.load_token(FRIEND_ACCOUNT) is None
    assert server.store.account_business(FRIEND_ACCOUNT) is None


def test_data_deletion_removes_token_and_history():
    _reset()
    client.get(f"/auth/instagram/callback?code=the-code&state={_invite()}")
    biz_id, _ = server.store.account_business(FRIEND_ACCOUNT)
    server.store.log_conversation(biz_id, ConversationRecord("555", timestamp=1.0))
    r = client.post("/auth/instagram/delete", data={"signed_request": _signed({"user_id": FRIEND_ACCOUNT})})
    assert r.status_code == 200
    body = r.json()
    assert body["confirmation_code"] and body["url"].startswith("https://itsmia.info/auth/instagram/deletion")
    assert server.store.load_token(FRIEND_ACCOUNT) is None
    assert server.store.conversations(biz_id) == []


def test_summary_endpoints_need_the_admin_secret_and_work_per_business():
    _reset()
    sent = []

    async def fake_send(slug=None):
        sent.append(slug)
    real_send = server._send_daily_summary
    server._send_daily_summary = fake_send
    try:
        assert client.post("/trigger-summary").status_code == 403
        assert client.get("/preview-summary").status_code == 403
        admin = {"X-Admin-Secret": "admin-pw"}
        assert client.post("/trigger-summary?slug=nope", headers=admin).status_code == 404
        assert client.post("/trigger-summary?slug=lash-lab", headers=admin).status_code == 200
        assert client.post("/trigger-summary", headers=admin).status_code == 200
        assert sent == ["lash-lab", "glam-by-dang"]
    finally:
        server._send_daily_summary = real_send

    biz_id = server.store.ensure_business("lash-lab", "Lash Lab")
    server.store.log_conversation(biz_id, ConversationRecord("555", "Jo", "asked about lifts", timestamp=1.0))
    r = client.get("/preview-summary?slug=lash-lab", headers=admin)
    assert r.status_code == 200 and "asked about lifts" in "".join(r.json()["chunks"])
    r = client.get("/preview-summary", headers=admin)
    assert "asked about lifts" not in "".join(r.json()["chunks"])
    assert server.store.conversations(biz_id)  # preview doesn't clear


def test_admin_delete_data_disconnects_and_clears_history():
    _reset()
    server.business_id()  # Kha's business exists, as in production
    client.get(f"/auth/instagram/callback?code=the-code&state={_invite()}")
    biz_id, _ = server.store.account_business(FRIEND_ACCOUNT)
    server.store.log_conversation(biz_id, ConversationRecord("555", timestamp=1.0))
    assert client.post("/admin/delete-data", json={"slug": "lash-lab"}).status_code == 403
    r = client.post("/admin/delete-data", json={"slug": "lash-lab"}, headers={"X-Admin-Secret": "admin-pw"})
    assert r.status_code == 200 and r.json()["accounts_disconnected"] == 1
    assert server.store.load_token(FRIEND_ACCOUNT) is None
    assert server.store.account_business(FRIEND_ACCOUNT) is None
    assert server.store.conversations(biz_id) == []
    assert server._account_for(FRIEND_ACCOUNT) is None


def test_reviewer_link_is_reusable_for_30_days():
    _reset()
    r = client.post("/admin/api/login", json={"secret": "admin-pw"})
    assert r.status_code == 200
    r = client.post("/admin/api/businesses/lash-lab/invite", json={"reviewer": True})
    assert r.status_code == 200, r.text
    invite = parse_qs(urlparse(r.json()["url"]).query)["invite"][0]
    for _ in range(2):  # a reviewer can connect, disconnect and connect again
        r = client.get(f"/auth/instagram/callback?code=the-code&state={invite}")
        assert r.status_code == 200, r.text
    assert server.store.account_business(FRIEND_ACCOUNT)[1] == "lash-lab"
    clock = server.store.clock
    try:
        server.store.clock = lambda: clock() + 29 * 24 * 3600
        assert client.get(f"/connect?invite={invite}").status_code == 200
        server.store.clock = lambda: clock() + 31 * 24 * 3600
        assert client.get(f"/connect?invite={invite}").status_code == 404
    finally:
        server.store.clock = clock
    client.post("/admin/api/logout")


def test_older_databases_get_the_reusable_invite_column():
    import sqlite3
    path = os.path.join(_tmp.name, "old.db")
    old = sqlite3.connect(path)
    old.executescript("CREATE TABLE invites (token_hash TEXT PRIMARY KEY, business_id INTEGER NOT NULL, "
                      "created_at REAL NOT NULL, expires_at REAL NOT NULL, used_at REAL, account_id TEXT);"
                      "INSERT INTO invites VALUES ('h', 1, 0, 9e12, NULL, NULL);")
    old.close()
    s = Store(path)
    biz = s.ensure_business("lash-lab", "Lash Lab")
    s.create_invite("new", biz, 9e12, reusable=True)
    s.use_invite("new", "acct")
    s.use_invite("h", "acct")
    assert s.open_invite("new") == (biz, "lash-lab") and s.open_invite("h") is None


def test_homepage_and_privacy_and_no_public_simulator():
    r = client.get("/")
    assert r.status_code == 200 and "Instagram DMs, answered" in r.text and "/privacy" in r.text
    assert client.get("/simulator").status_code == 404
    assert client.post("/chat", json={"message": "hi"}).status_code in (404, 405)
    assert "Privacy policy" in client.get("/privacy").text


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
