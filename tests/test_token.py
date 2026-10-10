#!/usr/bin/env python3
"""
Tests for Instagram token persistence and refresh scheduling.
No network or API keys needed.
Run: python3 tests/test_token.py
"""
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from instagram_token import TokenStore, REFRESH_MARGIN, MIN_TOKEN_AGE

DAY = 24 * 60 * 60
PASSED = []
FAILED = []


def test(name):
    def decorator(fn):
        try:
            with tempfile.TemporaryDirectory() as d:
                fn(Path(d) / "meta_token.txt")
            PASSED.append(name)
            print(f"  ✓  {name}")
        except Exception:
            FAILED.append((name, traceback.format_exc()))
            print(f"  ✗  {name}\n{traceback.format_exc()}")
        return fn
    return decorator


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


print("\nInstagram token — test suite\n")


@test("1. First boot persists the env token")
def _(path):
    store = TokenStore(path, "env-a", Clock())
    store.load()
    assert store.token == "env-a"
    assert json.loads(path.read_text())["token"] == "env-a"


@test("2. Refreshed token survives a restart with the same env token")
def _(path):
    clock = Clock()
    store = TokenStore(path, "env-a", clock)
    store.load()
    store.apply_refresh("refreshed-1", 60 * DAY)
    restarted = TokenStore(path, "env-a", clock)
    restarted.load()
    assert restarted.token == "refreshed-1"
    assert restarted.expires_at == clock() + 60 * DAY


@test("3. A new env token overrides a stale persisted token")
def _(path):
    clock = Clock()
    store = TokenStore(path, "env-a", clock)
    store.load()
    store.apply_refresh("refreshed-1", 60 * DAY)
    restarted = TokenStore(path, "env-b", clock)
    restarted.load()
    assert restarted.token == "env-b"
    assert restarted.expires_at is None
    assert json.loads(path.read_text())["token"] == "env-b"


@test("4. Legacy plain-text file loses to a different env token")
def _(path):
    path.write_text("old-expired-token")
    store = TokenStore(path, "env-new", Clock())
    store.load()
    assert store.token == "env-new"


@test("5. Legacy plain-text file is used when no env token is set")
def _(path):
    path.write_text("old-token")
    store = TokenStore(path, "", Clock())
    store.load()
    assert store.token == "old-token"


@test("6. Unknown expiry → refresh once the token is 24h old")
def _(path):
    clock = Clock()
    store = TokenStore(path, "env-a", clock)
    store.load()
    assert store.seconds_until_refresh() == MIN_TOKEN_AGE
    clock.t += MIN_TOKEN_AGE + 1
    assert store.seconds_until_refresh() == 0


@test("7. Known expiry → refresh REFRESH_MARGIN before it, and survives restarts")
def _(path):
    clock = Clock()
    store = TokenStore(path, "env-a", clock)
    store.load()
    store.apply_refresh("refreshed-1", 60 * DAY)
    assert store.seconds_until_refresh() == 60 * DAY - REFRESH_MARGIN
    clock.t += 20 * DAY
    restarted = TokenStore(path, "env-a", clock)
    restarted.load()
    assert restarted.seconds_until_refresh() == 40 * DAY - REFRESH_MARGIN


@test("8. Health note: quiet when healthy, loud on failure, near expiry, or expired")
def _(path):
    clock = Clock()
    store = TokenStore(path, "env-a", clock)
    store.load()
    store.apply_refresh("refreshed-1", 60 * DAY)
    assert store.health_note() == ""
    store.record_failure("refresh failed with HTTP 400")
    assert "refresh failed" in store.health_note()
    store.apply_refresh("refreshed-2", 60 * DAY)
    assert store.health_note() == ""
    clock.t += 55 * DAY
    assert "not refreshed" in store.health_note()
    clock.t += 6 * DAY
    assert "expired" in store.health_note()


print(f"\n{len(PASSED)} passed, {len(FAILED)} failed\n")
sys.exit(1 if FAILED else 0)
