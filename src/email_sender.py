"""Sends email (plain text, plus an optional branded HTML version) through Resend (https://resend.com).

Set RESEND_API_KEY and SUMMARY_FROM_EMAIL (e.g. "Mia <mia@yourdomain.com>", on a
domain verified in Resend) in Railway. Without them, email is off and callers fall
back to whatever they did before.
"""
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"


def email_configured() -> bool:
    return bool(os.environ.get("RESEND_API_KEY") and os.environ.get("SUMMARY_FROM_EMAIL"))


async def send_email(to: str, subject: str, text: str, html: Optional[str] = None) -> bool:
    """Returns True once Resend accepts the email."""
    if not email_configured():
        logger.warning("Email not configured (RESEND_API_KEY / SUMMARY_FROM_EMAIL) — not sending to %s", to)
        return False
    payload = {"from": os.environ["SUMMARY_FROM_EMAIL"], "to": [to], "subject": subject, "text": text}
    if html:
        payload["html"] = html
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            resp = await http.post(
                RESEND_URL,
                headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"},
                json=payload,
            )
    except httpx.HTTPError as e:
        logger.error("Email to %s could not reach Resend: %s", to, e)
        return False
    if resp.status_code >= 300:
        logger.error("Email to %s failed %s: %s", to, resp.status_code, resp.text)
        return False
    logger.info("Email sent to %s (%s)", to, subject)
    return True
