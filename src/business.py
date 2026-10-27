"""Per-business configuration loaded from businesses/<slug>/.

Each business folder holds:
- business.json  settings: names, timezone, hours, links, canned replies, cancellation tiers,
                 where and when the owner's daily summary goes, and how long Mia waits
                 before replying
- about.md       who the assistant works for, and its tone
- rules.md       the business's reply rules and links section
- context.md     facts and prices the assistant answers from

src/prompt_template.md holds the parts every business shares (categories, the JSON
output contract, action rules) and slots the business's files in. Placeholders are
written {{name}}.

Businesses can also be created or edited in the admin panel. Those settings live in
the database (see use_saved_settings) and take precedence over a repo folder with the
same slug, so a business edited in the panel no longer follows its folder.
"""
import json
import re
from dataclasses import dataclass
from datetime import datetime, time
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

BUSINESSES_DIR = Path(__file__).parent.parent / "businesses"
TEMPLATE_PATH = Path(__file__).parent / "prompt_template.md"
STARTER_DIR = Path(__file__).parent / "business_starter"
DEFAULT_BUSINESS_SLUG = "glam-by-dang"
FILE_NAMES = ("business.json", "about.md", "rules.md", "context.md")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# How long Mia waits after a client's last message before replying, so a client sending
# several messages in a row isn't interrupted and the owner has a chance to answer first.
DEFAULT_REPLY_DELAY_SECONDS = 5
# Waiting messages are held in memory, so a restart during the wait drops them; keep it short.
MAX_REPLY_DELAY_SECONDS = 300

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def _fill(text: str, values: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        if m.group(1) not in values:
            raise KeyError(f"Unknown placeholder {m.group(0)}")
        return values[m.group(1)]
    return _PLACEHOLDER.sub(sub, text)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").removesuffix("\n")


@dataclass(frozen=True)
class CancellationTier:
    min_hours: float
    status: str


@dataclass(frozen=True)
class Business:
    slug: str
    name: str
    owner_name: str
    assistant_name: str
    timezone: str
    city: str
    hours: dict[str, tuple[time, time]]
    booking_link: str
    review_link: str
    greeting: str
    escalation_reply: str
    out_of_hours_note: str
    cancellation_tiers: tuple[CancellationTier, ...]
    summary_email: str
    summary_hour: int
    reply_delay_seconds: int
    about: str
    rules: str
    context: str

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def is_open(self, now_local: datetime) -> bool:
        hours = self.hours.get(now_local.strftime("%A"))
        if not hours:
            return False
        open_t, close_t = hours
        return open_t <= now_local.time() < close_t

    def cancellation_status(self, hours_ahead: float) -> str:
        for tier in self.cancellation_tiers:
            if hours_ahead >= tier.min_hours:
                return tier.status
        return self.cancellation_tiers[-1].status

    @cached_property
    def system_prompt(self) -> str:
        values = {
            "owner": self.owner_name,
            "assistant": self.assistant_name,
            "business": self.name,
            "booking_link": self.booking_link,
            "review_link": self.review_link,
        }
        sections = {
            "about": _fill(self.about, values),
            "rules": _fill(self.rules, values),
            "context": _fill(self.context, values),
        }
        return _fill(_read(TEMPLATE_PATH) + "\n", {**values, **sections})


def _parse_time(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


# Settings saved from the admin panel: slug -> {file name: text}. Set by the server.
_saved_get: Callable[[str], Optional[dict[str, str]]] = lambda slug: None
_saved_slugs: Callable[[], list[str]] = lambda: []


def use_saved_settings(get: Callable[[str], Optional[dict[str, str]]],
                       slugs: Callable[[], list[str]]) -> None:
    global _saved_get, _saved_slugs
    _saved_get, _saved_slugs = get, slugs
    load_business.cache_clear()


def _folder_files(folder: Path) -> dict[str, str]:
    return {name: (folder / name).read_text(encoding="utf-8") for name in FILE_NAMES}


def business_files(slug: str) -> dict[str, str]:
    """The four files a business is built from: the panel's saved copy, else its repo folder."""
    saved = _saved_get(slug)
    if saved is not None:
        return saved
    folder = BUSINESSES_DIR / slug
    if not (folder / "business.json").is_file():
        raise KeyError(f"No business {slug!r}")
    return _folder_files(folder)


def business_source(slug: str) -> str:
    return "panel" if _saved_get(slug) is not None else "repo"


def starter_files() -> dict[str, str]:
    """A blank business to fill in, for the admin panel's "new business"."""
    return _folder_files(STARTER_DIR)


def build_business(slug: str, files: dict[str, str]) -> Business:
    """Build and check a business; raises ValueError with a readable reason if it's broken."""
    try:
        cfg = json.loads(files["business.json"])
        tiers = sorted(
            (CancellationTier(float(t["min_hours"]), t["status"]) for t in cfg.get("cancellation_tiers", [])),
            key=lambda t: t.min_hours,
            reverse=True,
        )
        if not tiers:
            raise ValueError("Add at least one cancellation tier")
        ZoneInfo(cfg["timezone"])
        summary_hour = int(cfg.get("summary_hour", 10))
        if not 0 <= summary_hour <= 23:
            raise ValueError("summary_hour must be between 0 and 23")
        reply_delay_seconds = int(cfg.get("reply_delay_seconds", DEFAULT_REPLY_DELAY_SECONDS))
        if not 0 <= reply_delay_seconds <= MAX_REPLY_DELAY_SECONDS:
            raise ValueError(f"reply_delay_seconds must be between 0 and {MAX_REPLY_DELAY_SECONDS}")
        business = Business(
            slug=slug,
            name=cfg["name"],
            owner_name=cfg["owner_name"],
            assistant_name=cfg["assistant_name"],
            timezone=cfg["timezone"],
            city=cfg["city"],
            hours={day: (_parse_time(o), _parse_time(c)) for day, (o, c) in cfg["hours"].items()},
            booking_link=cfg["booking_link"],
            review_link=cfg["review_link"],
            greeting=cfg["greeting"],
            escalation_reply=cfg["escalation_reply"],
            out_of_hours_note=cfg["out_of_hours_note"],
            cancellation_tiers=tuple(tiers),
            summary_email=cfg.get("summary_email", ""),
            summary_hour=summary_hour,
            reply_delay_seconds=reply_delay_seconds,
            about=files["about.md"].removesuffix("\n"),
            rules=files["rules.md"].removesuffix("\n"),
            context=files["context.md"].removesuffix("\n"),
        )
        business.system_prompt  # fails on an unknown {{placeholder}}
    except json.JSONDecodeError as e:
        raise ValueError(f"business.json isn't valid JSON: {e}") from None
    except KeyError as e:
        what = str(e.args[0]) if e.args else str(e)
        if what.startswith("Unknown placeholder"):
            raise ValueError(f"{what}. Placeholders you can use: {{{{owner}}}}, {{{{assistant}}}}, "
                             f"{{{{business}}}}, {{{{booking_link}}}}, {{{{review_link}}}}") from None
        raise ValueError(f"Missing setting: {what}") from None
    except Exception as e:
        if isinstance(e, ValueError):
            raise
        raise ValueError(f"{type(e).__name__}: {e}") from None
    return business


@lru_cache(maxsize=None)
def load_business(slug: str) -> Business:
    return build_business(slug, business_files(slug))


def business_slugs() -> list[str]:
    """Every business: repo folders in businesses/ plus any created in the admin panel."""
    folders = {p.parent.name for p in BUSINESSES_DIR.glob("*/business.json")}
    return sorted(folders | set(_saved_slugs()))


def default_business() -> Business:
    return load_business(DEFAULT_BUSINESS_SLUG)
