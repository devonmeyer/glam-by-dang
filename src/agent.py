import json
import re
import logging
from datetime import datetime, timedelta
from anthropic import Anthropic
from typing import Optional
from business import Business, default_business

logger = logging.getLogger(__name__)
client = Anthropic()

MAX_HISTORY = 20


def parse_json(text: str) -> dict:
    text = text.strip()
    # Try stripping a single code fence wrapping the whole response
    clean = re.sub(r"^```(?:json)?\s*", "", text)
    clean = re.sub(r"\s*```$", "", clean).strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass
    # Model sometimes "thinks out loud" and outputs multiple JSON blocks.
    # Extract all fenced blocks and try the last one (the model's final answer).
    blocks = re.findall(r"```(?:json)?\s*([\s\S]*?)```", text)
    for block in reversed(blocks):
        try:
            return json.loads(block.strip())
        except json.JSONDecodeError:
            continue
    # Last resort: find the last {...} object in the raw text
    for m in reversed(list(re.finditer(r'\{[\s\S]*?\}(?=\s*$|\s*```)', text))):
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            continue
    raise json.JSONDecodeError("No valid JSON found", text, 0)


def process_message(
    message: str,
    history: list[dict],
    is_new_conversation: bool = True,
    business: Optional[Business] = None,
) -> dict:
    """Single API call: classify + generate response. Returns a result dict for the server."""
    business = business or default_business()
    messages = [
        {"role": m["role"], "content": m["content"]}
        for m in history[-MAX_HISTORY:]
    ]
    messages.append({"role": "user", "content": message})

    now_nyc = datetime.now(business.tz)
    fmt = "%A, %B %-d at %-I:%M %p %Z"
    cutoff_24h  = (now_nyc + timedelta(hours=24)).strftime(fmt)
    cutoff_7d   = (now_nyc + timedelta(days=7)).strftime(fmt)
    day_map = "\n".join(
        f"  {(now_nyc + timedelta(days=i)).strftime('%A')} = {(now_nyc + timedelta(days=i)).strftime('%B %-d, %Y')}"
        for i in range(8)
    )
    today = now_nyc.strftime("%A, %B %-d, %Y at %-I:%M %p %Z")
    outside_business_hours = not business.is_open(now_nyc)
    # Pre-classify each upcoming day against cancellation policy thresholds
    # so the model never has to do date arithmetic itself
    now_ts = now_nyc.timestamp()
    def policy_for_day(days_ahead: int) -> str:
        t = (now_nyc + timedelta(days=days_ahead)).timestamp()
        return business.cancellation_status((t - now_ts) / 3600)

    policy_map = "\n".join(
        f"  {(now_nyc + timedelta(days=i)).strftime('%A, %B %-d')} → {policy_for_day(i)}"
        for i in range(8)
    )
    date_note = f"The current date and time in {business.city} is {today}."
    if business.cancellation_tiers:
        date_note += (
            f"\n\nCANCELLATION POLICY STATUS BY DAY — use these directly, do not recalculate:\n{policy_map}\n\n"
            f"Example: if a client says their appointment is 'Saturday', look up Saturday above and apply that policy tier exactly."
        )

    booking_link_already_shared = any(business.booking_link in m.get("content", "") for m in history)
    out_of_hours_note_already_sent = any(business.out_of_hours_note in m.get("content", "") for m in history)
    should_send_out_of_hours_note = outside_business_hours and not out_of_hours_note_already_sent

    prefix_note = (
        "IMPORTANT: This is the start of a new conversation (first contact or returning after time away). "
        f"You MUST begin your reply text with exactly \"{business.greeting}\""
        if is_new_conversation else
        "This is an ongoing back-and-forth — do NOT re-introduce yourself."
    )

    link_context_note = (
        "The booking link has already been shared earlier in this conversation. "
        "If you want to reference booking, say something like 'you can use the link I shared above' or 'the booking link above' — "
        "do NOT write anything that implies you are about to share a new link (e.g. 'grab a spot right here', 'here's the link', etc.)."
        if booking_link_already_shared else
        "The booking link has not been shared yet in this conversation."
    )

    for attempt in range(2):
        system = [
            {"type": "text", "text": business.system_prompt, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": date_note},
            {"type": "text", "text": prefix_note},
            {"type": "text", "text": link_context_note},
            {
                "type": "text",
                "text": "Reminder: respond with ONLY the JSON object described above — no prose, "
                        "no markdown fences, nothing before or after it. This applies even when the "
                        "answer requires careful date/policy reasoning — do the reasoning, then put "
                        "only the final answer in the \"reply\" field of the JSON.",
            },
        ]
        if attempt == 1:
            system.append({
                "type": "text",
                "text": "IMPORTANT: Your previous response was not valid JSON. Return ONLY a valid JSON object — no markdown, no explanation, no extra text before or after the JSON.",
            })
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system,
            messages=messages,
        )
        raw_text = response.content[0].text
        try:
            raw = parse_json(raw_text)
            break
        except json.JSONDecodeError:
            logger.warning("JSON parse failed (attempt %d). Raw:\n%s", attempt + 1, raw_text)
            if attempt == 1:
                stripped = raw_text.strip()
                if stripped and not stripped.startswith("{") and not stripped.startswith("```"):
                    # Model answered correctly in plain prose but dropped the JSON wrapper.
                    # Use the prose directly rather than crashing into the generic escalation
                    # fallback, which would replace a correct answer with a non-answer.
                    logger.warning("Falling back to raw prose reply after JSON failures")
                    raw = {
                        "category": "faq",
                        "frustration_level": "none",
                        "confidence": "low",
                        "action": "reply",
                        "booking_signal": False,
                        "reply": stripped,
                        "link": None,
                        "escalation": None,
                        "conversation_summary": "",
                        "actions_taken": ["Answered (fallback: non-JSON model response)"],
                    }
                else:
                    raise

    category = raw.get("category", "escalate")
    frustration_level = raw.get("frustration_level", "none")
    is_frustrated = frustration_level in ("medium", "high")
    action = raw.get("action", "escalate")
    reply = raw.get("reply")
    link = raw.get("link")
    escalation = raw.get("escalation")
    confidence = raw.get("confidence", "low")
    conversation_summary = raw.get("conversation_summary", "")
    actions_taken = raw.get("actions_taken", [])

    # Suppress the booking link if it was already sent in this conversation
    if link == business.booking_link:
        already_sent = any(business.booking_link in m.get("content", "") for m in history)
        if already_sent:
            link = None

    logger.info(
        "category=%s frustration=%s action=%s confidence=%s booking_signal=%s",
        category, frustration_level, action, confidence, raw.get("booking_signal"),
    )

    classification = {"confidence": confidence, "frustration_level": frustration_level}

    if action == "silent":
        return {
            "action": "silent",
            "category": category,
            "classification": classification,
            "messages": None,
            "escalation": None,
            "conversation_summary": conversation_summary,
            "actions_taken": actions_taken,
        }

    if action == "reply":
        msgs = [reply] if reply else []
        if link:
            msgs.append(link)
        if msgs and should_send_out_of_hours_note:
            msgs.append(business.out_of_hours_note)
        return {
            "action": "reply",
            "category": category,
            "classification": classification,
            "messages": msgs or None,
            "escalation": None,
            "conversation_summary": conversation_summary,
            "actions_taken": actions_taken,
        }

    # escalate
    msgs = [reply] if reply else [business.escalation_reply]
    if should_send_out_of_hours_note:
        msgs.append(business.out_of_hours_note)
    return {
        "action": "escalate",
        "category": category,
        "classification": classification,
        "messages": msgs,
        "escalation": escalation,
        "conversation_summary": conversation_summary,
        "actions_taken": actions_taken,
    }
