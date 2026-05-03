#!/usr/bin/env python3
"""
CLI simulation for the Glam by Dang Instagram DM agent.
Usage: python cli.py
"""

import os
import sys
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent.parent / ".env")

if not os.environ.get("ANTHROPIC_API_KEY"):
    print("Error: ANTHROPIC_API_KEY not set. Add it to .env or export it.")
    sys.exit(1)

from agent import process_message

CATEGORY_LABELS = {
    "faq": "FAQ",
    "thankyou": "Thank You",
    "booking_intent": "Booking Intent",
    "complaint": "Complaint",
    "escalate": "Escalation Required",
}

COLORS = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "cyan": "\033[96m",
    "green": "\033[92m",
    "yellow": "\033[93m",
    "red": "\033[91m",
    "dim": "\033[2m",
    "magenta": "\033[95m",
}


def c(color: str, text: str) -> str:
    return f"{COLORS.get(color, '')}{text}{COLORS['reset']}"


def print_separator():
    print(c("dim", "─" * 60))


def print_classification(classification: dict, category: str):
    label = CATEGORY_LABELS.get(category, category)
    confidence = classification.get("confidence", "?")
    reason = classification.get("reason", "")

    color = "green" if category in ("faq", "thankyou", "booking_intent") else "red"
    print(c("dim", f"  Category: ") + c(color, c("bold", label)) + c("dim", f"  [{confidence} confidence]"))
    print(c("dim", f"  Reason:   {reason}"))


def print_reply(reply: str):
    print()
    print(c("cyan", c("bold", "  BOT REPLY:")))
    for line in reply.strip().split("\n"):
        print(c("cyan", f"  {line}"))


def print_escalation(escalation: dict):
    urgency = escalation.get("urgency", "unknown").upper()
    urgency_color = "red" if urgency == "HIGH" else "yellow"

    print()
    print(c("red", c("bold", "  ⚠  ESCALATION ALERT — Kha needs to reply")))
    print(c(urgency_color, f"  Urgency:          {urgency}"))
    print(c("dim",          f"  Summary:          ") + escalation.get("summary", ""))
    print(c("dim",          f"  Suggested action: ") + escalation.get("suggested_action", ""))
    print(c("dim",          f"  Why escalated:    ") + escalation.get("reason_escalated", ""))


def run():
    history = []
    print()
    print(c("magenta", c("bold", "  Glam by Dang — Instagram DM Simulator")))
    print(c("dim", "  Type a customer message and press Enter. Type 'reset' to start a new conversation, 'quit' to exit."))
    print()

    while True:
        try:
            user_input = input(c("bold", "Customer: ")).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        if not user_input:
            continue

        if user_input.lower() == "quit":
            print("Goodbye.")
            break

        if user_input.lower() == "reset":
            history = []
            print(c("dim", "  [Conversation reset]\n"))
            continue

        print()
        print_separator()

        result = process_message(user_input, history)

        print_classification(result["classification"], result["category"])

        if result["action"] == "reply":
            print_reply(result["reply"])
            history.append({"role": "user", "content": user_input})
            history.append({"role": "assistant", "content": result["reply"]})
        else:
            print_escalation(result["escalation"])
            history.append({"role": "user", "content": user_input})

        print_separator()
        print()


if __name__ == "__main__":
    run()
