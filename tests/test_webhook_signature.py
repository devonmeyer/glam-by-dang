#!/usr/bin/env python3
"""
Tests for Instagram webhook X-Hub-Signature-256 verification.
No network or API key needed.
Run: python3 tests/test_webhook_signature.py   (or: pytest tests/test_webhook_signature.py)
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

SECRET = "test-app-secret"
# Non-instagram object: passes verification, then returns early with no side effects.
BODY = json.dumps({"object": "page", "entry": []}).encode()

client = TestClient(server.app)


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(headers: dict, secret: str = SECRET):
    server.META_APP_SECRET = secret
    return client.post("/webhook", content=BODY, headers=headers)


def test_valid_signature_accepted():
    r = _post({"X-Hub-Signature-256": _sign(BODY)})
    assert r.status_code == 200, r.text


def test_missing_signature_rejected():
    r = _post({})
    assert r.status_code == 403, r.text


def test_wrong_secret_rejected():
    r = _post({"X-Hub-Signature-256": _sign(BODY, "other-secret")})
    assert r.status_code == 403, r.text


def test_tampered_body_rejected():
    sig = _sign(BODY)
    server.META_APP_SECRET = SECRET
    r = client.post("/webhook", content=BODY + b" ", headers={"X-Hub-Signature-256": sig})
    assert r.status_code == 403, r.text


def test_malformed_header_rejected():
    r = _post({"X-Hub-Signature-256": "garbage"})
    assert r.status_code == 403, r.text


def test_unset_secret_rejects_everything():
    r = _post({"X-Hub-Signature-256": _sign(BODY, "")}, secret="")
    assert r.status_code == 403, r.text


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ✗  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
