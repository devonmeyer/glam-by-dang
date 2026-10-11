"""Branded HTML versions of the emails Mia sends (daily summary, connect link).

Email clients ignore most CSS, so everything is inline and laid out with tables.
Colors and type follow static/brand/brand.css. The logo is a PNG served from
PUBLIC_BASE_URL; without it the header falls back to a text wordmark.
"""
from html import escape
from typing import Iterable

import instagram_connect

BLUSH = "#F7EFEC"
TINT = "#F3E3DE"
LINE = "#E6D3CC"
BERRY = "#8E3550"
ROSE = "#C9A39A"
BERRY_TINT = "#F6DCE3"
COCOA = "#3A2724"
SOFT = "#7A625D"
DISPLAY = "'Bodoni Moda', Didot, 'Bodoni 72', Georgia, serif"
BODY = "Figtree, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"

FONTS = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
         'family=Bodoni+Moda:opsz,wght@6..96,500&family=Figtree:wght@400;600&display=swap">')


def _logo() -> str:
    base = instagram_connect.base_url()
    if base:
        return (f'<img src="{escape(base)}/static/brand/email-logo.png" width="119" height="52" alt="Mia" '
                f'style="display:block;border:0;width:119px;height:52px">')
    return f'<span style="font-family:{DISPLAY};font-size:32px;line-height:1;color:{COCOA}">mia</span>'


def layout(heading: str, body_html: str, preheader: str = "") -> str:
    """The shared frame: logo, a white card with a serif heading, and a quiet footer."""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">{FONTS}</head>
<body style="margin:0;padding:0;background:{BLUSH}">
<div style="display:none;max-height:0;overflow:hidden">{escape(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{BLUSH}">
<tr><td align="center" style="padding:28px 16px 40px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px">
<tr><td style="padding:0 4px 16px">{_logo()}</td></tr>
<tr><td style="background:#ffffff;border:1px solid {LINE};border-radius:12px;padding:28px 28px 30px;font-family:{BODY};font-size:15px;line-height:1.6;color:{COCOA}">
<h1 style="margin:0 0 16px;font-family:{DISPLAY};font-weight:500;font-size:26px;line-height:1.2;color:{COCOA}">{escape(heading)}</h1>
{body_html}
</td></tr>
<tr><td style="padding:16px 4px 0;font-family:{BODY};font-size:12px;line-height:1.5;color:{SOFT}">Sent by Mia · itsmia.info</td></tr>
</table></td></tr></table></body></html>"""


def button(label: str, url: str) -> str:
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:20px 0"><tr>'
            f'<td style="background:{BERRY};border-radius:999px">'
            f'<a href="{escape(url)}" style="display:inline-block;padding:13px 22px;font-family:{BODY};font-size:15px;'
            f'font-weight:600;color:#ffffff;text-decoration:none">{escape(label)}</a></td></tr></table>')


def _stat(number: int, label: str, bg: str) -> str:
    return (f'<td width="50%" style="background:{bg};border-radius:8px;padding:12px 14px;font-family:{BODY};color:{COCOA}">'
            f'<div style="font-family:{DISPLAY};font-size:26px;line-height:1.1">{number}</div>'
            f'<div style="font-size:13px;color:{SOFT}">{escape(label)}</div></td>')


def _items(rows: Iterable[tuple[str, str, str]], accent: str) -> str:
    out = []
    for name, summary, actions in rows:
        out.append(f'<tr><td style="border-left:3px solid {accent};padding:2px 0 2px 12px;font-family:{BODY};font-size:14px;'
                   f'line-height:1.55;color:{COCOA}"><strong>{escape(name)}</strong> {escape(summary)}'
                   f'<div style="color:{SOFT};font-size:13px">{escape(actions)}</div></td></tr>'
                   f'<tr><td style="height:12px;font-size:0;line-height:0">&nbsp;</td></tr>')
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(out)}</table>'


def _section(title: str) -> str:
    return (f'<p style="margin:22px 0 10px;font-family:{BODY};font-size:12px;font-weight:600;letter-spacing:.08em;'
            f'text-transform:uppercase;color:{SOFT}">{escape(title)}</p>')


def summary(assistant_name: str, owner_name: str, business_name: str, date_str: str, token_note: str,
            needs_you: list[tuple[str, str, str]], handled: list[tuple[str, str, str]]) -> str:
    """Each row is (who, what the conversation was about, what Mia did)."""
    body = [f'<p style="margin:0">Good morning, {escape(owner_name)}. Here\'s what came into '
            f'{escape(business_name)}\'s DMs.</p>']
    if token_note:
        body.append(f'<p style="margin:16px 0 0;background:{BERRY_TINT};border-radius:8px;padding:10px 12px;'
                    f'font-size:14px">{escape(token_note)}</p>')
    if not needs_you and not handled:
        body.append(f'<p style="margin:16px 0 0;color:{SOFT}">No DMs to report today.</p>')
    else:
        body.append(f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:18px">'
                    f'<tr>{_stat(len(needs_you), "need you", BERRY_TINT)}<td width="10">&nbsp;</td>'
                    f'{_stat(len(handled), f"handled by {assistant_name}", TINT)}</tr></table>')
        if needs_you:
            body.append(_section("Needs you") + _items(needs_you, BERRY))
        if handled:
            body.append(_section("For your visibility") + _items(handled, ROSE))
    preheader = f"{len(needs_you)} need you, {len(handled)} handled" if (needs_you or handled) else "No DMs today"
    return layout(f"Your summary for {date_str}", "".join(body), preheader)


def invite(owner_name: str, business_name: str, assistant_name: str, url: str) -> str:
    body = (f'<p style="margin:0">Hi {escape(owner_name)},</p>'
            f'<p style="margin:12px 0 0">Here\'s your private link to connect {escape(business_name)}\'s Instagram '
            f'to {escape(assistant_name)}. Open it, log in with your business Instagram account, and tap Allow.</p>'
            + button("Connect Instagram", url) +
            f'<p style="margin:0;color:{SOFT};font-size:13px">The link works once and expires in 7 days. '
            f'If the button doesn\'t work, paste this into your browser:<br>'
            f'<a href="{escape(url)}" style="color:{BERRY};word-break:break-all">{escape(url)}</a></p>')
    return layout(f"Connect your Instagram to {assistant_name}", body, f"Your private link for {business_name}")
