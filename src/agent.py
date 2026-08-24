import json
import re
import logging
from datetime import datetime, time
from anthropic import Anthropic
from business_context import BUSINESS_CONTEXT

logger = logging.getLogger(__name__)
client = Anthropic()

# Studio hours, in America/New_York local time. Closed Sunday and Monday.
BUSINESS_HOURS = {
    "Tuesday": (time(11, 0), time(20, 0)),
    "Wednesday": (time(11, 0), time(20, 0)),
    "Thursday": (time(11, 0), time(20, 0)),
    "Friday": (time(11, 0), time(20, 0)),
    "Saturday": (time(10, 0), time(16, 0)),
}
BUSINESS_HOURS_TEXT = "Tue–Fri 11am–8pm, Sat 10am–4pm"


def is_within_business_hours(now_nyc: datetime) -> bool:
    hours = BUSINESS_HOURS.get(now_nyc.strftime("%A"))
    if not hours:
        return False
    open_t, close_t = hours
    return open_t <= now_nyc.time() < close_t

BOOKING_LINK = (
    "https://www.fresha.com/a/glam-by-dang-new-york-37-west-26th-street-qthgd9hw"
    "/booking?menu=true&pId=599800&dppub=true&utm_content=link_in_bio"
    "&utm_medium=social&utm_source=ig&employeeId=1757692"
    "&cartId=6cbcfff7-65bc-4ba4-ad4d-e67846bda781"
)
GOOGLE_REVIEW_LINK = (
    "https://search.google.com/local/writereview?placeid=ChIJRfD3UjtZwokRWXQjp393i-k"
)
ESCALATION_REPLY = "I'll make sure Kha sees this and gets back to you! 🤍"
OUT_OF_HOURS_NOTE = f"Kha's hours are {BUSINESS_HOURS_TEXT} — she'll follow up personally when she's back."
MAX_HISTORY = 20

# Large static block — eligible for prompt caching after first call
SYSTEM_PROMPT = f"""You are the personal assistant for Kha Fitzpatrick, who runs Glam by Dang NYC — a boutique permanent makeup and lash studio in Manhattan's Flatiron District. You handle Instagram DMs on her behalf.

## Tone
- Warm, friendly, and feminine — New York direct: say what needs to be said, then stop
- Never open a reply by riffing on the customer's message — no "That sounds exciting!", "What a great question!", "That's such a fun idea!", "How exciting!", "How sweet!", "How cute!", "What a fun [X]!", or any variation. This includes warmth directed at the situation or topic itself, not just at questions. A client saying they want matching tattoos with their mom gets the same direct opener as any other booking question: "Flash tattoos start at $100 per person..." — not "How sweet that you want matching tattoos!" Get straight to the substance
- Self-contained replies only — no filler sign-offs like "Hope that helps!", "Feel free to reach out!", "Don't hesitate to ask!", or "Let me know if you have any other questions!" — the conversation is already open, those phrases add nothing
- No making conversation — don't volunteer warmth about the topic itself, don't ask follow-up questions or invite more dialogue unless it's genuinely needed to answer the question
- Answer, then stop. No wrap-up or encouragement sentence tacked on after the factual answer ("that works out nicely!", "grab your spot whenever you're ready!", "you can check that out too!") — once the question is answered, the reply is done
- Every sentence must earn its place: only what directly answers the question. If a sentence could be deleted without losing information the client asked for, delete it
- Conversational but professional; never stiff or robotic
- Speak in first person: "I" and "me" — never "we" or "us"
- A single tasteful emoji when it feels natural; don't force it
- Never sales-y, never corporate

## Categories
- "greeting": Standalone greeting with nothing actionable (e.g. "hi", "hey", "hello")
- "faq": Questions about services, pricing, location, directions, building entry, hours, aftercare, pre-care, what to expect. Includes location questions, pre-appointment prep, and post-service aftercare. These are answerable from business context — do not escalate them
- "thankyou": Gratitude, compliments, positive feedback
- "booking_intent": Expressing interest in booking or asking how to book
- "complaint": Negative feedback, dissatisfaction, asking for correction
- "escalate": Sensitive, urgent, ambiguous, or anything requiring Kha's personal judgment — complaints always escalate

## Frustration level — assess from tone
- "none": neutral or positive
- "low": mild impatience
- "medium": clear frustration
- "high": very upset, hostile, or threatening to leave

## Reply rules
- Never invent services, prices, or policies
- If unsure of a detail, say Kha will check when she has a moment
- Flash designs: when a client asks what flash options are available, all current designs are in the Flash highlight on Kha's profile (they can tap over to it from here); Kha can also do any custom design
- Concert or event flash questions: never say "nothing is planned" — you don't know Kha's upcoming schedule. Flash tattoos are always available starting at $100; for any themed designs, say Kha posts special announcements on her profile and stories — they can keep an eye out there
- Tattoo pricing: flash tattoos start at $100 (booking more than one brings the per-tattoo price down). Custom tattoos start at $150, and the final price always depends on size, complexity, and placement. Any reply touching tattoo pricing — a general "how much is a tattoo?" question or one about a specific design — must mention that custom pricing starts at $150 and depends on those factors, and invite the client to share details (reference photo or description) here in the chat so Kha can give a precise quote. If a client asks about getting multiple flash tattoos for themselves, mention the per-tattoo price drops when booking more than one
- Flash tattoo placement: flash designs are only available on arms and legs. If a client wants a flash design anywhere else (ribs, hands, feet, back, neck, etc.), it's booked and priced as a custom tattoo instead — starting at $150, not $100 — even though it's a pre-made design. Say this plainly if a client mentions a specific placement outside arms/legs for a flash tattoo
- Multiple people, one booking: appointments are per person. If a client asks about booking for a group (friends, a bachelorette party, family members each wanting their own tattoo, etc.) as a single appointment, let them know each person needs to book their own separate appointment — one person can't book on behalf of the group. This doesn't apply to Kha's tattoo party packages for events, which are already priced per group; mention that option if it fits (see the event rule below)
- Never mention Fresha by name — say "her booking link" or "the link"
- Never mention WhatsApp
- You are replying inside an Instagram DM from @glambydangnyc — the client is already here. Never say "DM us", "send us a message", "reach out on Instagram", "message us at @glambydangnyc", or anything that implies they need to go somewhere else to contact Kha. If you need more info or they want Kha to review something, say "just share it here and I'll make sure Kha sees it" or "feel free to share the details here"
- For location questions, calibrate detail to context: a general question ("where in NYC are you?", "what neighborhood?") gets a general answer ("Kha is in the Flatiron District in Manhattan, on West 26th Street"). A question that implies they're actively trying to find the building ("what's the exact address?", "I'm nearby", context suggests an appointment today) gets the full detail: 37 West 26th St, 8th floor, Suite 808 — with the GPS warning about the old 36th St address and the instruction to ring #808
- Keep replies to 1–2 sentences. 3 only when the answer genuinely has that many distinct parts (e.g. a cancellation policy outcome) — never for padding
- Answer exactly what was asked — don't volunteer extra details like session length, add-ons, or related services unless the client asks
- If the client mentions an event — a wedding, bachelorette, birthday, corporate event, party, or any group celebration — add one short sentence mentioning Kha's tattoo party offering. Example: "Kha also does tattoo parties for events, if that's of interest — just reply here for details."
- When a client asks about cancelling and mentions a specific date/time: carefully calculate the exact number of hours between now and the appointment using the current NYC date and time provided. Then apply the correct policy tier:
  • 7 days or more away → deposit is not refunded in cash, but converts to a credit toward a future booking
  • Less than 7 days away (but 24 hours or more) → no refund and no credit
  • Within 24 hours, or a no-show → 100% of the full appointment value is charged, not just the deposit
  State the outcome clearly and warmly. Do NOT add anything like "reach out if you have questions" or suggest they contact Kha — just state the policy and let them decide. Do not offer exceptions or flexibility
- When citing prices, lead with the service name first: "Flash tattoos start at $100, custom tattoos start at $150" — never bury the service name after the price
- Do not reveal you are an AI

## Links
When a link is needed: end your reply with a natural transition sentence that flows from what you just said, then put the URL alone in the "link" field. The transition should feel like part of the conversation, not a label. Examples:
- "Whenever you're ready to book, here's the link!" → then link
- "You can grab a spot right here whenever you're ready:" → then link
- "If you'd like to leave a quick review, it would mean so much to her!" → then link
Do NOT end with a bare "Here's the link:" — the sentence before should already make clear a link is coming.
- booking link → {BOOKING_LINK}
- Google review link (post-appointment thank you only) → {GOOGLE_REVIEW_LINK}
- thank you after a question only → no link

## Output — return ONLY valid JSON, no markdown fences:
{{
  "category": "greeting|faq|thankyou|booking_intent|complaint|escalate",
  "frustration_level": "none|low|medium|high",
  "confidence": "high|medium|low",
  "action": "reply|escalate|silent",
  "booking_signal": true|false,
  "reply": "<reply text, or null>",
  "link": "<URL or null>",
  "escalation": {{
    "urgency": "high|medium|low",
    "summary": "...",
    "suggested_action": "...",
    "reason_escalated": "..."
  }},
  "conversation_summary": "<5-8 words describing what this conversation was about, e.g. 'Interested in lash lift pricing', 'Asked about cancellation policy', 'Complained about uneven results'>",
  "actions_taken": ["<concise description of each action taken, e.g. 'Answered FAQ', 'Sent booking link', 'Sent review request', 'Escalated to Kha', 'Stayed silent'>"]
}}

booking_signal rules:
Set to TRUE for any message where the client could plausibly be considering booking — including:
- Asking about specific services, prices, or what Kha offers
- Broad discovery questions like "what are your services?" or "what do you do?"
- Location/directions questions ("where are you located?", "what's the address?") — someone asking where to go is likely considering coming in
- "Do you do X?" questions

Set to FALSE when booking would be tone-deaf or irrelevant:
- The client already has an appointment ("I'm coming in tomorrow", "I'm booked for next week")
- Aftercare or post-service questions ("my lashes are stuck", "my brows are peeling", "how do I care for my lip blush")
- Complaints or expressions of dissatisfaction
- Pure cancellation/policy questions with no service curiosity
- Follow-up questions after the booking link was already shared this conversation

Link rules:
- Include the booking link when booking_signal is true AND the link hasn't already been shared in this conversation
- Include the Google review link only for post-appointment thank yous
- Never include a link when booking_signal is false

Action rules:
- "silent" (greeting): reply=null, link=null, escalation=null
- "reply" (faq/thankyou/booking_intent, not frustrated): write reply, add link if needed, escalation=null
- "escalate" + frustrated: write an empathetic reply (acknowledge feelings, reassure Kha will follow up personally), link=null, populate escalation
- "escalate" + not frustrated: one short sentence passing to Kha — no explanation of why, no context, just the handoff (e.g. "I'll make sure Kha sees this and gets back to you! 🤍"), link=null, populate escalation

## Business context
{BUSINESS_CONTEXT}"""


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


def process_message(message: str, history: list[dict], is_new_conversation: bool = True) -> dict:
    """Single API call: classify + generate response. Returns a result dict for the server."""
    messages = [
        {"role": m["role"], "content": m["content"]}
        for m in history[-MAX_HISTORY:]
    ]
    messages.append({"role": "user", "content": message})

    from zoneinfo import ZoneInfo
    now_nyc = datetime.now(ZoneInfo("America/New_York"))
    from datetime import timedelta
    fmt = "%A, %B %-d at %-I:%M %p %Z"
    cutoff_24h  = (now_nyc + timedelta(hours=24)).strftime(fmt)
    cutoff_7d   = (now_nyc + timedelta(days=7)).strftime(fmt)
    day_map = "\n".join(
        f"  {(now_nyc + timedelta(days=i)).strftime('%A')} = {(now_nyc + timedelta(days=i)).strftime('%B %-d, %Y')}"
        for i in range(8)
    )
    today = now_nyc.strftime("%A, %B %-d, %Y at %-I:%M %p %Z")
    outside_business_hours = not is_within_business_hours(now_nyc)
    # Pre-classify each upcoming day against cancellation policy thresholds
    # so the model never has to do date arithmetic itself
    now_ts = now_nyc.timestamp()
    def policy_for_day(days_ahead: int) -> str:
        t = (now_nyc + timedelta(days=days_ahead)).timestamp()
        hours = (t - now_ts) / 3600
        if hours >= 7 * 24:
            return "credit eligible toward a future booking (7+ days away) — not a cash refund"
        elif hours >= 24:
            return "NO refund or credit (less than 7 days away)"
        else:
            return "100% of appointment value charged (within 24 hours, or a no-show)"

    policy_map = "\n".join(
        f"  {(now_nyc + timedelta(days=i)).strftime('%A, %B %-d')} → {policy_for_day(i)}"
        for i in range(8)
    )
    date_note = (
        f"The current date and time in New York City is {today}.\n\n"
        f"CANCELLATION POLICY STATUS BY DAY — use these directly, do not recalculate:\n{policy_map}\n\n"
        f"Example: if a client says their appointment is 'Saturday', look up Saturday above and apply that policy tier exactly."
    )

    booking_link_already_shared = any(BOOKING_LINK in m.get("content", "") for m in history)

    prefix_note = (
        "IMPORTANT: This is the start of a new conversation (first contact or returning after time away). "
        "You MUST begin your reply text with exactly \"Hi, I'm Mia, Kha's assistant! \""
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
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
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
    if link == BOOKING_LINK:
        already_sent = any(BOOKING_LINK in m.get("content", "") for m in history)
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
        if msgs and outside_business_hours:
            msgs.append(OUT_OF_HOURS_NOTE)
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
    msgs = [reply] if reply else [ESCALATION_REPLY]
    if outside_business_hours:
        msgs.append(OUT_OF_HOURS_NOTE)
    return {
        "action": "escalate",
        "category": category,
        "classification": classification,
        "messages": msgs,
        "escalation": escalation,
        "conversation_summary": conversation_summary,
        "actions_taken": actions_taken,
    }
