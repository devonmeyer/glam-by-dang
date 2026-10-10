"""Per-business configuration loaded from businesses/<slug>/.

Each business folder holds:
- business.json  settings: names, timezone, hours, links, canned replies, cancellation tiers,
                 and where and when the owner's daily summary goes
- about.md       who the assistant works for, and its tone
- rules.md       the business's reply rules and links section
- context.md     facts and prices the assistant answers from

src/prompt_template.md holds the parts every business shares (categories, the JSON
output contract, action rules) and slots the business's files in. Placeholders are
written {{name}}.
"""
import json
import re
from dataclasses import dataclass
from datetime import datetime, time
from functools import cached_property, lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

BUSINESSES_DIR = Path(__file__).parent.parent / "businesses"
TEMPLATE_PATH = Path(__file__).parent / "prompt_template.md"
DEFAULT_BUSINESS_SLUG = "glam-by-dang"

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


@lru_cache(maxsize=None)
def load_business(slug: str) -> Business:
    folder = BUSINESSES_DIR / slug
    cfg = json.loads((folder / "business.json").read_text(encoding="utf-8"))
    tiers = sorted(
        (CancellationTier(t["min_hours"], t["status"]) for t in cfg.get("cancellation_tiers", [])),
        key=lambda t: t.min_hours,
        reverse=True,
    )
    return Business(
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
        summary_hour=cfg.get("summary_hour", 10),
        about=_read(folder / "about.md"),
        rules=_read(folder / "rules.md"),
        context=_read(folder / "context.md"),
    )


def business_slugs() -> list[str]:
    """Every business with a folder in businesses/."""
    return sorted(p.parent.name for p in BUSINESSES_DIR.glob("*/business.json"))


def default_business() -> Business:
    return load_business(DEFAULT_BUSINESS_SLUG)
