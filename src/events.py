"""Event management: the line at a party (e.g. Kha's wedding tattoo parties).

Guests join by submitting the business's JotForm waiver, which calls our webhook; the owner
runs the line from the panel, and a public page plus an iPad display show it live.
"""
import io
import json
import re
from typing import Optional

import segno

STATUSES = ("waiting", "now", "done", "no_show")
VENMO_RE = re.compile(r"^[A-Za-z0-9_-]{1,30}$")
MAX_NAME = 60


def clean_name(s: str) -> str:
    return " ".join(str(s or "").split())[:MAX_NAME]


def public_name(first: str, last: str) -> str:
    """First name and last initial, which is all the public line shows."""
    last = last.strip()
    return f"{first} {last[0].upper()}." if last else first


def split_full_name(full: str) -> tuple[str, str]:
    parts = clean_name(full).split(" ")
    return (parts[0], " ".join(parts[1:])) if len(parts) > 1 else (parts[0], "")


def jotform_guest_name(raw_request: str, pretty: str = "") -> Optional[tuple[str, str]]:
    """(first, last) from a JotForm submission, or None if no name field is found.

    JotForm's rawRequest is JSON keyed like "q3_fullName"; its Full Name field is an object
    with "first" and "last". Separate first/last text fields and a single name field also work.
    """
    try:
        data = json.loads(raw_request or "{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    first = last = ""
    for key, value in data.items():
        name = re.sub(r"^q\d+_", "", str(key)).lower()
        if isinstance(value, dict) and ("first" in value or "last" in value):
            first, last = clean_name(value.get("first", "")), clean_name(value.get("last", ""))
            if first:
                return first, last
        elif isinstance(value, str) and "name" in name:
            if "first" in name:
                first = first or clean_name(value)
            elif "last" in name:
                last = last or clean_name(value)
            elif not first and value.strip():
                first, last = split_full_name(value)
    if first:
        return first, last
    # "pretty" is "Full Name:Jane Doe, Email:..." — the last resort
    match = re.search(r"(?:^|,\s*)[^:,]*name[^:,]*:([^,]+)", pretty or "", re.IGNORECASE)
    if match and match.group(1).strip():
        return split_full_name(match.group(1))
    return None


def venmo_handle(s: str) -> str:
    """'@glambydangnyc' or a venmo.com link becomes 'glambydangnyc'. Raises ValueError if invalid."""
    s = (s or "").strip()
    s = re.sub(r"^(https?://)?(account\.)?venmo\.com/(u/)?", "", s, flags=re.IGNORECASE).lstrip("@").strip("/")
    if s and not VENMO_RE.match(s):
        raise ValueError("Enter a Venmo username like @glambydangnyc")
    return s


def qr_svg(url: str) -> str:
    """A standalone SVG (with its namespace, so it works as an <img>)."""
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=10, border=2, dark="#3A2724", light="#ffffff",
                                     xmldecl=False)
    return buf.getvalue().decode()
