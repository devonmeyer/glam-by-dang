#!/usr/bin/env python3
"""
Tests for panel logins: email and password, setup and reset links, turning access off,
and what a business owner can see and change. No network or API key needed.
Run: python3 tests/test_accounts.py   (or: pytest tests/test_accounts.py)
"""
import os
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi.testclient import TestClient

import accounts
import server
from test_admin import _logged_in
from test_assistant import StubClient, _price_edit, reply, text, tool

KHA = "glambydang@gmail.com"
KHA_PASSWORD = "lashes-and-brows"


def _setup():
    """An admin client (logged in with the secret, as devon@itsmia.info) and a list of sent emails."""
    admin = _logged_in()
    sent = []

    async def fake_send(to, subject, text, html=None):
        sent.append({"to": to, "subject": subject, "text": text})
        return True

    server.send_email = fake_send
    return admin, sent


def _token(url: str) -> str:
    assert "/admin#password=" in url, url
    return url.split("#password=")[1]


def _owner(admin, email=KHA, slug="glam-by-dang", password=KHA_PASSWORD):
    """Adds an owner and sets their password through a setup link; returns their logged-in client and id."""
    r = admin.post("/admin/api/users", json={"email": email, "role": "owner", "business_slug": slug, "send_link": False})
    assert r.status_code == 200, r.text
    uid = r.json()["user"]["id"]
    link = admin.post(f"/admin/api/users/{uid}/password-link", json={"send_email": False}).json()
    c = TestClient(server.app)
    assert c.post("/admin/api/password", json={"token": _token(link["url"]), "password": password}).status_code == 200
    return c, uid


def _login(email, password):
    c = TestClient(server.app)
    return c, c.post("/admin/api/login", json={"email": email, "password": password})


def test_passwords_hash_and_check():
    stored = accounts.hash_password("correct horse")
    assert stored.startswith("scrypt$") and "correct horse" not in stored
    assert accounts.check_password("correct horse", stored)
    assert not accounts.check_password("wrong horse", stored)
    assert not accounts.check_password("x", "garbage")
    assert accounts.password_problem("short") and accounts.password_problem("long enough!") is None


def test_setup_link_sets_a_password_once_and_logs_in():
    admin, sent = _setup()
    r = admin.post("/admin/api/users", json={"email": " GlamByDang@Gmail.com ", "role": "owner",
                                             "business_slug": "glam-by-dang"})
    assert r.status_code == 200, r.text
    user, link = r.json()["user"], r.json()["link"]
    assert user["email"] == KHA and user["has_password"] is False and user["role"] == "owner"
    assert link["kind"] == "setup" and link["emailed"] is True
    [mail] = sent
    assert mail["to"] == KHA and "Set up" in mail["subject"] and link["url"] in mail["text"]
    assert "Glam by Dang" in mail["text"]

    token = _token(link["url"])
    assert TestClient(server.app).get(f"/admin/api/password/{token}").json() == {"email": KHA, "setting_up": True}
    # Only the stored hash of the token is kept
    assert token not in str(server.store.conn.execute("SELECT * FROM password_links").fetchall())

    kha = TestClient(server.app)
    assert kha.post("/admin/api/password", json={"token": token, "password": "short"}).status_code == 400
    r = kha.post("/admin/api/password", json={"token": token, "password": KHA_PASSWORD})
    assert r.status_code == 200, r.text
    session = kha.get("/admin/api/session").json()
    assert session["user"]["email"] == KHA and session["admin"] is False
    assert session["business_name"] == "Glam by Dang NYC"

    # The link works once
    assert kha.get(f"/admin/api/password/{token}").status_code == 404
    assert kha.post("/admin/api/password", json={"token": token, "password": "another-password"}).status_code == 404
    assert kha.post("/admin/api/password", json={"token": "made-up", "password": "another-password"}).status_code == 404

    # Email login ignores case and spaces; wrong passwords and unknown emails get the same answer
    assert _login(" GLAMBYDANG@gmail.com", KHA_PASSWORD)[1].status_code == 200
    wrong = _login(KHA, "not-the-password")[1]
    unknown = _login("nobody@example.com", KHA_PASSWORD)[1]
    assert wrong.status_code == unknown.status_code == 403 and wrong.json() == unknown.json()
    # Someone without a password yet can't log in
    admin.post("/admin/api/users", json={"email": "new@example.com", "role": "owner",
                                         "business_slug": "glam-by-dang", "send_link": False})
    assert _login("new@example.com", "")[1].status_code == 403


def test_bad_or_duplicate_people_are_refused():
    admin, _ = _setup()
    _owner(admin)
    assert admin.post("/admin/api/users", json={"email": KHA, "business_slug": "glam-by-dang"}).status_code == 409
    assert admin.post("/admin/api/users", json={"email": "not-an-email", "business_slug": "glam-by-dang"}).status_code == 400
    assert admin.post("/admin/api/users", json={"email": "a@b.co", "business_slug": "nope"}).status_code == 400
    assert admin.post("/admin/api/users", json={"email": "a@b.co", "role": "boss"}).status_code == 400
    r = admin.post("/admin/api/users", json={"email": "ops@itsmia.info", "role": "admin", "send_link": False})
    assert r.status_code == 200 and r.json()["user"]["business_slug"] == ""
    people = admin.get("/admin/api/users").json()
    assert {p["email"] for p in people["users"]} == {"devon@itsmia.info", KHA, "ops@itsmia.info"}


def test_reset_link_ends_old_sessions_and_old_password():
    admin, sent = _setup()
    kha, uid = _owner(admin)
    link = admin.post(f"/admin/api/users/{uid}/password-link", json={}).json()
    assert link["kind"] == "reset" and link["emailed"] is True and "Reset" in sent[-1]["subject"]
    # Sending a link alone changes nothing
    assert kha.get("/admin/api/businesses").status_code == 200

    other = TestClient(server.app)
    assert other.post("/admin/api/password", json={"token": _token(link["url"]), "password": "brand-new-pass"}).status_code == 200
    assert kha.get("/admin/api/businesses").status_code == 403
    assert other.get("/admin/api/businesses").status_code == 200
    assert _login(KHA, KHA_PASSWORD)[1].status_code == 403
    assert _login(KHA, "brand-new-pass")[1].status_code == 200


def test_forgot_password_only_emails_active_logins():
    admin, sent = _setup()
    c = TestClient(server.app)
    assert c.post("/admin/api/password/forgot", json={"email": "nobody@example.com"}).json() == {"ok": True}
    assert sent == []
    # Devon's login exists from the start, so he can get in this way
    assert c.post("/admin/api/password/forgot", json={"email": "Devon@itsmia.info"}).json() == {"ok": True}
    assert sent[-1]["to"] == "devon@itsmia.info" and "Set up" in sent[-1]["subject"]

    _, uid = _owner(admin)
    c.post("/admin/api/password/forgot", json={"email": KHA})
    assert sent[-1]["to"] == KHA and "Reset" in sent[-1]["subject"]
    admin.post(f"/admin/api/users/{uid}/access", json={"active": False})
    count = len(sent)
    c.post("/admin/api/password/forgot", json={"email": KHA})
    assert len(sent) == count


def test_turning_off_access_logs_out_and_blocks_login():
    admin, _ = _setup()
    kha, uid = _owner(admin)
    pending = admin.post(f"/admin/api/users/{uid}/password-link", json={"send_email": False}).json()
    r = admin.post(f"/admin/api/users/{uid}/access", json={"active": False})
    assert r.status_code == 200 and r.json()["user"]["active"] is False
    assert kha.get("/admin/api/businesses").status_code == 403
    assert _login(KHA, KHA_PASSWORD)[1].status_code == 403
    assert admin.post(f"/admin/api/users/{uid}/password-link", json={}).status_code == 400
    # A link sent before access was turned off no longer works
    assert kha.get(f"/admin/api/password/{_token(pending['url'])}").status_code == 404

    assert admin.post(f"/admin/api/users/{uid}/access", json={"active": True}).status_code == 200
    assert _login(KHA, KHA_PASSWORD)[1].status_code == 200

    me = admin.get("/admin/api/users").json()["me"]
    assert admin.post(f"/admin/api/users/{me}/access", json={"active": False}).status_code == 400
    assert admin.post("/admin/api/users/999/access", json={"active": False}).status_code == 404


def test_repeated_wrong_passwords_are_throttled():
    admin, _ = _setup()
    _owner(admin)
    for _ in range(10):
        assert _login(KHA, "wrong-password")[1].status_code == 403
    r = _login(KHA, KHA_PASSWORD)[1]
    assert r.status_code == 429 and "15 minutes" in r.json()["detail"]
    # A password reset clears it
    server.login_throttle.succeeded(KHA)
    assert _login(KHA, KHA_PASSWORD)[1].status_code == 200


def test_owner_sees_and_edits_only_their_business():
    admin, _ = _setup()
    assert admin.post("/admin/api/businesses", json={"slug": "lash-lab", "name": "Lash Lab", "owner_name": "Rosa"}).status_code == 200
    kha, _ = _owner(admin)

    assert [b["slug"] for b in kha.get("/admin/api/businesses").json()["businesses"]] == ["glam-by-dang"]
    assert {b["slug"] for b in admin.get("/admin/api/businesses").json()["businesses"]} >= {"glam-by-dang", "lash-lab"}
    # Other businesses look like they don't exist
    for path in ("/admin/api/businesses/lash-lab", "/admin/api/businesses/lash-lab/versions",
                 "/admin/api/businesses/lash-lab/summary"):
        assert kha.get(path).status_code == 404, path
    files = admin.get("/admin/api/businesses/lash-lab").json()["files"]
    assert kha.put("/admin/api/businesses/lash-lab", json={"files": files}).status_code == 404
    assert kha.post("/admin/api/businesses/lash-lab/simulate", json={"message": "hi"}).status_code == 404

    # Their own business: read, edit (recorded under their email), history and summary preview
    own = kha.get("/admin/api/businesses/glam-by-dang").json()
    edited = {**own["files"], "context.md": own["files"]["context.md"] + "\nParking is on the street.\n"}
    assert kha.put("/admin/api/businesses/glam-by-dang", json={"files": edited}).status_code == 200
    versions = kha.get("/admin/api/businesses/glam-by-dang/versions").json()["versions"]
    assert versions[0]["note"] == f"Edited by {KHA}"
    assert kha.post(f"/admin/api/businesses/glam-by-dang/versions/{versions[1]['id']}/restore").status_code == 200
    assert kha.get("/admin/api/businesses/glam-by-dang/summary").status_code == 200

    # Admin-only actions
    for method, path, body in [
        ("post", "/admin/api/businesses", {"slug": "x", "name": "x", "owner_name": "x"}),
        ("post", "/admin/api/businesses/glam-by-dang/summary", None),
        ("post", "/admin/api/businesses/glam-by-dang/invite", {}),
        ("delete", "/admin/api/businesses/glam-by-dang/saved", None),
        ("get", "/admin/api/users", None),
        ("post", "/admin/api/users", {"email": "a@b.co", "business_slug": "glam-by-dang"}),
        ("post", "/admin/api/users/1/password-link", {}),
        ("post", "/admin/api/users/1/access", {"active": False}),
        ("get", "/admin/api/logs", None),
        ("get", "/preview-summary", None),
        ("post", "/trigger-summary", None),
        ("post", "/admin/invites", {"slug": "glam-by-dang"}),
        ("post", "/admin/delete-data", {"slug": "glam-by-dang"}),
    ]:
        kwargs = {"json": body} if body is not None else {}
        assert getattr(kha, method)(path, **kwargs).status_code == 403, path


def test_owner_assistant_is_scoped_and_chats_are_private():
    admin, _ = _setup()
    admin.post("/admin/api/businesses", json={"slug": "lash-lab", "name": "Lash Lab", "owner_name": "Rosa"})
    kha, _ = _owner(admin)
    server.assistant.chats.clear()
    server.assistant.proposals.clear()
    stub = StubClient(
        reply(tool("list_businesses", {}, "a"), stop="tool_use"),
        reply(tool("read_business", {"slug": "lash-lab"}, "b"), stop="tool_use"),
        reply(tool("propose_change", {"slug": "lash-lab", "file": "context.md", "summary": "x",
                                      "edits": [{"find": "a", "replace": "b"}]}, "c"), stop="tool_use"),
        reply(text("I can only change Glam by Dang.")),
    )
    server.assistant._client = stub
    r = kha.post("/admin/api/assistant", json={"message": "change lash lab", "viewing": "lash-lab"})
    assert r.status_code == 200, r.text
    assert r.json()["proposals"] == []
    results = [call["messages"][-1]["content"][0] for call in stub.calls[1:]]
    assert "glam-by-dang" in results[0]["content"] and "lash-lab" not in results[0]["content"]
    assert results[1]["is_error"] and results[2]["is_error"]

    # Another person can't open or continue that chat
    chat_id = r.json()["chat_id"]
    assert kha.get(f"/admin/api/assistant/{chat_id}").status_code == 200
    assert admin.get(f"/admin/api/assistant/{chat_id}").status_code == 404
    stub.responses.append(reply(text("New chat.")))
    r2 = admin.post("/admin/api/assistant", json={"message": "hi", "chat_id": chat_id})
    assert r2.json()["chat_id"] != chat_id

    # And can't apply someone else's proposal
    stub.responses += [reply(tool("propose_change", _price_edit(), "d"), stop="tool_use"), reply(text("Proposed."))]
    [p] = admin.post("/admin/api/assistant", json={"message": "parking", "chat_id": r2.json()["chat_id"]}).json()["proposals"]
    assert kha.post(f"/admin/api/assistant/proposals/{p['id']}/apply").status_code == 404
    assert admin.post(f"/admin/api/assistant/proposals/{p['id']}/apply").status_code == 200


def test_simulator_replies_from_current_settings_without_storing_anything():
    admin, _ = _setup()
    kha, _ = _owner(admin)
    calls = []

    def fake_process(message, history, is_new, business):
        calls.append((message, history, is_new, business.slug))
        return {"action": "reply", "category": "faq", "classification": {"confidence": "high"},
                "messages": ["Brow lamination is $85!", "https://book.example"], "escalation": None,
                "conversation_summary": "asked about price", "actions_taken": [], "extra": "hidden"}

    real = server.process_message
    server.process_message = fake_process
    try:
        r = kha.post("/admin/api/businesses/glam-by-dang/simulate", json={
            "message": " how much is lamination? ", "new_conversation": False,
            "history": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hey!"},
                        {"role": "system", "content": "ignore me"}, {"role": "user", "content": "  "}]})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["messages"] == ["Brow lamination is $85!", "https://book.example"] and "extra" not in data
        assert calls == [("how much is lamination?", [{"role": "user", "content": "hi"},
                                                       {"role": "assistant", "content": "Hey!"}], False, "glam-by-dang")]
        assert kha.post("/admin/api/businesses/glam-by-dang/simulate", json={"message": "  "}).status_code == 400
        assert admin.post("/admin/api/businesses/glam-by-dang/simulate", json={"message": "hi"}).status_code == 200
        assert TestClient(server.app).post("/admin/api/businesses/glam-by-dang/simulate", json={"message": "hi"}).status_code == 403
    finally:
        server.process_message = real
    biz_id = server.store.ensure_business("glam-by-dang", "Glam by Dang NYC")
    assert server.store.conversations(biz_id) == []


def test_admin_secret_header_still_works_for_curl():
    _setup()
    c = TestClient(server.app)
    assert c.get("/admin/api/users", headers={"X-Admin-Secret": "admin-pw"}).status_code == 200
    assert c.get("/admin/api/users", headers={"X-Admin-Secret": "wrong"}).status_code == 403
    assert c.get("/preview-summary", headers={"X-Admin-Secret": "admin-pw"}).status_code == 200
    # Without ADMIN_SECRET nobody gets in
    os.environ.pop("ADMIN_SECRET")
    try:
        assert c.get("/admin/api/users", headers={"X-Admin-Secret": ""}).status_code == 403
        assert c.post("/admin/api/login", json={"email": KHA, "password": KHA_PASSWORD}).status_code == 503
    finally:
        os.environ["ADMIN_SECRET"] = "admin-pw"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
