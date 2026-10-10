"""Business Login for Instagram: the calls that turn an owner's consent into a token.

Flow: Mia sends the owner to authorize_url(); Instagram redirects back with a code;
exchange_code() trades it for a short-lived token, long_lived() for a 60-day one,
whoami() reads the account ids, and subscribe() turns on DM webhooks for the account.

Needs INSTAGRAM_APP_ID (the Instagram app id from the Meta dashboard), META_APP_SECRET
(the Instagram app secret, already used for webhook signatures) and PUBLIC_BASE_URL
(e.g. https://itsmia.info), whose /auth/instagram/callback must be listed as a valid
OAuth redirect URI in the dashboard.
"""
import base64
import hashlib
import hmac
import json
import os
from typing import Optional
from urllib.parse import urlencode

import httpx

GRAPH_API_BASE = "https://graph.instagram.com/v25.0"
SCOPES = "instagram_business_basic,instagram_business_manage_messages"


class ConnectError(Exception):
    """Instagram refused a step; the message is safe to show the owner."""


def app_id() -> str:
    return os.environ.get("INSTAGRAM_APP_ID", "")


def app_secret() -> str:
    return os.environ.get("META_APP_SECRET", "")


def base_url() -> str:
    return os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")


def connect_configured() -> bool:
    return bool(app_id() and app_secret() and base_url())


def redirect_uri() -> str:
    return f"{base_url()}/auth/instagram/callback"


def authorize_url(state: str) -> str:
    return "https://www.instagram.com/oauth/authorize?" + urlencode({
        "client_id": app_id(),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
    })


async def exchange_code(http: httpx.AsyncClient, code: str) -> str:
    resp = await http.post("https://api.instagram.com/oauth/access_token", data={
        "client_id": app_id(),
        "client_secret": app_secret(),
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri(),
        "code": code,
    })
    if resp.status_code != 200 or "access_token" not in resp.json():
        raise ConnectError(f"Instagram didn't accept the login (HTTP {resp.status_code}).")
    return resp.json()["access_token"]


async def long_lived(http: httpx.AsyncClient, short_token: str) -> tuple[str, Optional[float]]:
    resp = await http.get("https://graph.instagram.com/access_token", params={
        "grant_type": "ig_exchange_token",
        "client_secret": app_secret(),
        "access_token": short_token,
    })
    if resp.status_code != 200 or "access_token" not in resp.json():
        raise ConnectError(f"Instagram didn't issue a long-lived token (HTTP {resp.status_code}).")
    data = resp.json()
    return data["access_token"], data.get("expires_in")


async def whoami(http: httpx.AsyncClient, token: str) -> dict:
    """{'user_id': id webhooks carry, 'id': app-scoped id, 'username': ...}"""
    resp = await http.get(f"{GRAPH_API_BASE}/me", params={"fields": "user_id,username"},
                          headers={"Authorization": f"Bearer {token}"})
    if resp.status_code != 200 or not resp.json().get("user_id"):
        raise ConnectError(f"Couldn't read the Instagram account (HTTP {resp.status_code}).")
    return resp.json()


async def subscribe(http: httpx.AsyncClient, token: str) -> None:
    resp = await http.post(f"{GRAPH_API_BASE}/me/subscribed_apps",
                           params={"subscribed_fields": "messages", "access_token": token})
    if resp.status_code != 200:
        raise ConnectError(f"Couldn't turn on DM notifications for the account (HTTP {resp.status_code}).")


def parse_signed_request(signed_request: str) -> Optional[dict]:
    """Verify and decode the signed_request Meta posts to deauthorize/deletion callbacks."""
    try:
        sig_b64, payload_b64 = signed_request.split(".", 1)
        sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
        expected = hmac.new(app_secret().encode(), payload_b64.encode(), hashlib.sha256).digest()
        if not app_secret() or not hmac.compare_digest(sig, expected):
            return None
        return json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
    except (ValueError, json.JSONDecodeError):
        return None
