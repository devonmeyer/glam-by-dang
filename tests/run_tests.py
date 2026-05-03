#!/usr/bin/env python3
"""
Test suite for Glam by Dang DM assistant.
Tests agent.py directly — no server required, no debounce delay.
Run: python3 tests/run_tests.py
"""
import sys
import os
import traceback

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent import process_message, BOOKING_LINK, GOOGLE_REVIEW_LINK, ESCALATION_REPLY

PASSED = []
FAILED = []


def test(name):
    def decorator(fn):
        try:
            fn()
            PASSED.append(name)
            print(f"  ✓  {name}")
        except AssertionError as e:
            FAILED.append((name, str(e)))
            print(f"  ✗  {name}: {e}")
        except Exception as e:
            FAILED.append((name, traceback.format_exc()))
            print(f"  ✗  {name}: {type(e).__name__}: {e}")
        return fn
    return decorator


def msgs(result):
    return " ".join(result.get("messages") or [])


print("\nGlam by Dang — test suite\n")


@test("1. Tattoo pricing → reply with both prices + booking link")
def _():
    r = process_message("how much for a tattoo?", [], is_new_conversation=True)
    assert r["action"] == "reply",          f"action={r['action']}"
    assert r["category"] == "faq",          f"category={r['category']}"
    text = msgs(r)
    assert "100" in text,                   "missing $100"
    assert "150" in text,                   "missing $150"
    assert BOOKING_LINK in text,            "missing booking link"


@test("2. Greeting → silent, no reply sent")
def _():
    r = process_message("hi", [], is_new_conversation=True)
    assert r["action"] == "silent",         f"action={r['action']}"
    assert r["category"] == "greeting",     f"category={r['category']}"
    assert r["messages"] is None,           "greeting should produce no messages"


@test("3. Post-appointment thank you → review link included")
def _():
    history = [
        {"role": "user",      "content": "I want to book a lash lift"},
        {"role": "assistant", "content": "Hi, I'm Mia, Kha's assistant! Here's the link:\n" + BOOKING_LINK},
    ]
    r = process_message(
        "Just got my lash lift done yesterday and I'm completely obsessed, thank you so much!",
        history,
        is_new_conversation=False,
    )
    assert r["action"] == "reply",          f"action={r['action']}"
    assert GOOGLE_REVIEW_LINK in msgs(r),   "expected review link for post-appointment thank you"


@test("4. Thank you after FAQ → no links, short reply")
def _():
    history = [
        {"role": "user",      "content": "how much is brow lamination?"},
        {"role": "assistant", "content": "Brow lamination is $150!"},
    ]
    r = process_message("thank you!", history, is_new_conversation=False)
    assert r["action"] == "reply",          f"action={r['action']}"
    text = msgs(r)
    assert BOOKING_LINK not in text,        "booking link should not appear after FAQ thank you"
    assert GOOGLE_REVIEW_LINK not in text,  "review link should not appear after FAQ thank you"


@test("5. Frustrated customer → escalate with empathy reply")
def _():
    r = process_message(
        "I sent a message 3 days ago and NOBODY has responded. This is really frustrating!!",
        [],
        is_new_conversation=True,
    )
    assert r["action"] == "escalate",       f"action={r['action']}"
    assert r["messages"],                   "frustrated client should still get a reply"
    assert r["escalation"],                 "escalation summary should be present"
    assert r["escalation"].get("urgency") in ("high", "medium"), \
        f"urgency={r['escalation'].get('urgency')}"


@test("6. Booking intent → reply with booking link")
def _():
    r = process_message(
        "I'd love to book a powder brow appointment, how do I do that?",
        [],
        is_new_conversation=True,
    )
    assert r["action"] == "reply",          f"action={r['action']}"
    assert BOOKING_LINK in msgs(r),         "booking link should be present"


@test("7. Complaint → escalated to Kha")
def _():
    r = process_message(
        "Hi, I got a lash lift last week and my lashes have already lost their curl. I'm really disappointed.",
        [],
        is_new_conversation=True,
    )
    assert r["action"] == "escalate",       f"action={r['action']}"
    assert r["escalation"],                 "escalation summary should be present"


@test("8. Returning customer with history → re-introduces as Mia")
def _():
    history = [
        {"role": "user",      "content": "Hi! I have an appointment tomorrow for lash extensions"},
        {"role": "assistant", "content": "Hi, I'm Mia, Kha's assistant! Kha is so excited to see you."},
        {"role": "user",      "content": "thank you! she did my brows last month and they're amazing"},
        {"role": "assistant", "content": "That's so wonderful to hear! 🤍"},
    ]
    r = process_message(
        "Hey it's been a while! Do you do fine line tattoos?",
        history,
        is_new_conversation=True,
    )
    assert r["action"] == "reply",          f"action={r['action']}"
    first = (r["messages"] or [""])[0].lower()
    assert "mia" in first,                  f"expected Mia intro, got: {first[:120]}"


@test("9. Ongoing conversation → no re-introduction")
def _():
    history = [
        {"role": "user",      "content": "how much is a lash lift?"},
        {"role": "assistant", "content": "Hi, I'm Mia, Kha's assistant! A lash lift is $175 ✨"},
    ]
    r = process_message("does it hurt?", history, is_new_conversation=False)
    assert r["action"] == "reply",          f"action={r['action']}"
    first = (r["messages"] or [""])[0].lower()
    assert "i'm mia" not in first,          f"should not re-introduce: {first[:120]}"


@test("10. Sensitive request → escalated with acknowledgment to client")
def _():
    r = process_message(
        "Hi, I need to speak with Kha directly about something private regarding my last appointment",
        [],
        is_new_conversation=True,
    )
    assert r["action"] == "escalate",       f"action={r['action']}"
    assert r["escalation"],                 "escalation info should be present"
    assert r.get("messages"),               "client should receive an acknowledgment message"


# ── Summary ───────────────────────────────────────────────────────────────────

total = len(PASSED) + len(FAILED)
print(f"\n{'─' * 44}")
print(f"  {len(PASSED)}/{total} passed", end="")

if FAILED:
    print(f"  ({len(FAILED)} failed)\n")
    for name, detail in FAILED:
        print(f"  FAIL: {name}")
        for line in detail.splitlines():
            print(f"        {line}")
    sys.exit(1)
else:
    print("  — all green ✓\n")
    sys.exit(0)
