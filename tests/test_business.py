#!/usr/bin/env python3
"""
Tests for per-business config loading and prompt rendering.
No network or API key needed.
Run: python3 tests/test_business.py   (or: pytest tests/test_business.py)
"""
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import business
from business import default_business, load_business


def test_kha_prompt_has_no_unfilled_placeholders():
    prompt = default_business().system_prompt
    assert "{{" not in prompt
    assert "Kha Fitzpatrick" in prompt
    assert default_business().booking_link in prompt
    assert "'Escalated to Kha'" in prompt



def test_prompt_routes_availability_questions_to_the_booking_link():
    prompt = default_business().system_prompt
    assert "## Availability questions" in prompt
    assert "asking about availability or openings" in prompt
    # The shared guidance must land before the business rules it explains
    assert prompt.index("## How to read the business's rules") < prompt.index("## Reply rules")

def test_hours():
    kha = default_business()
    tz = kha.tz
    assert kha.is_open(datetime(2026, 10, 13, 11, 0, tzinfo=tz))       # Tuesday open
    assert not kha.is_open(datetime(2026, 10, 13, 20, 0, tzinfo=tz))   # Tuesday close
    assert not kha.is_open(datetime(2026, 10, 12, 14, 0, tzinfo=tz))   # Monday closed
    assert kha.is_open(datetime(2026, 10, 17, 15, 59, tzinfo=tz))      # Saturday


def test_cancellation_tiers():
    kha = default_business()
    assert kha.cancellation_status(7 * 24).startswith("credit eligible")
    assert kha.cancellation_status(7 * 24 - 1).startswith("NO refund")
    assert kha.cancellation_status(24).startswith("NO refund")
    assert kha.cancellation_status(23.9).startswith("100%")


def test_second_business_gets_its_own_prompt():
    src = business.BUSINESSES_DIR / "glam-by-dang"
    with tempfile.TemporaryDirectory() as d:
        folder = Path(d) / "lash-lab"
        shutil.copytree(src, folder)
        cfg = json.loads((folder / "business.json").read_text())
        cfg.update(name="Lash Lab", owner_name="Rosa", booking_link="https://example.com/book")
        (folder / "business.json").write_text(json.dumps(cfg))
        (folder / "about.md").write_text("You are the assistant for {{owner}} at {{business}}.\n")
        (folder / "context.md").write_text("Lashes only.\n")

        original = business.BUSINESSES_DIR
        business.BUSINESSES_DIR = Path(d)
        try:
            rosa = load_business("lash-lab")
        finally:
            business.BUSINESSES_DIR = original

        prompt = rosa.system_prompt
        assert prompt.startswith("You are the assistant for Rosa at Lash Lab.")
        assert "'Escalated to Rosa'" in prompt
        assert "https://example.com/book" in prompt
        assert prompt.endswith("Lashes only.\n")
        assert "Kha Fitzpatrick" not in prompt


def test_unknown_placeholder_fails_loudly():
    try:
        business._fill("Hi {{nope}}", {"owner": "Kha"})
    except KeyError:
        return
    raise AssertionError("expected KeyError")


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
