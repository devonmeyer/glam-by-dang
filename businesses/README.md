# Businesses

Each folder here is one business Mia answers DMs for. The folder name is the business's slug.

| File | What goes in it |
| --- | --- |
| `business.json` | Names, timezone, opening hours, booking and review links, the greeting, escalation and out-of-hours lines, cancellation tiers, and `summary_email` / `summary_hour` (local time) for the owner's daily summary |
| `about.md` | Who the assistant works for, and its tone |
| `rules.md` | The business's reply rules and its links section |
| `context.md` | Facts and prices the assistant answers from |

`src/prompt_template.md` holds what every business shares (categories, the JSON output format, action rules) and slots these files in. In any of them, `{{owner}}`, `{{assistant}}`, `{{business}}`, `{{booking_link}}` and `{{review_link}}` are filled from `business.json`.

To add a business, copy `glam-by-dang/`, rewrite every file for the new business, then run `python3 tests/test_business.py`.
