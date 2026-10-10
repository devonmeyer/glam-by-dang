---
name: refresh-pricing
description: Use this skill when asked to check, refresh, sync, or audit Mia's pricing and policy info against Kha's live web presence — e.g. "check current pricing", "is our pricing up to date", "sync business context", "audit policies against the website". Fetches Kha's live booking page, website, and policy page, diffs them against Kha's business folder (businesses/glam-by-dang/context.md, rules.md and the cancellation tiers in business.json), and reports what changed before editing anything.
---

# Refresh pricing & policy context

`businesses/glam-by-dang/context.md` (prices and facts), the cancellation-tier rule in `businesses/glam-by-dang/rules.md`, and `cancellation_tiers` in `businesses/glam-by-dang/business.json` are a manually maintained snapshot of Kha's real pricing and policies. They drift from reality whenever Kha updates Fresha or her policy post without telling anyone. This skill re-checks the live sources and reports drift — it does not silently apply changes.

If Kha's business has been edited in the admin panel (https://itsmia.info/admin shows "Edited in panel"), the live settings are the panel's copy and the repo folder is ignored. In that case, report drift against what the panel shows and make approved changes in the panel, not in the repo.

## Live sources to check

- **Fresha booking page (source of truth for prices):**
  `https://www.fresha.com/a/glam-by-dang-new-york-37-west-26th-street-qthgd9hw`
  Fetching this only reliably returns a ~4-item "featured services" preview (duration + price, including "from $X" phrasing) — not the full 45-service catalog. That's a known limitation, not a fetch error; note it in the report rather than treating it as missing data.

- **Website:** `https://glambydang.com` (and `/pmu`, `/keratin`, `/lash`)
  Client-rendered SPA — WebFetch mostly returns nav/shell content, rarely line-item prices. Still worth checking for structural changes (new service categories, changed booking/contact links).

- **Instagram:** `https://www.instagram.com/glambydangnyc/`
  Bio text and link list only (no post/highlight content reachable via fetch). Check for a changed bio link.

- **Link-in-bio hub:** `https://bio.site/glambydang`
  Aggregates the current outbound links (booking, policies, socials, gift cards). Check this first if a URL below 404s — it's the fastest way to find the current link if Kha has moved something.

- **Policies page (source of truth for cancellation/deposit/no-show policy):**
  `https://lifewithkha.substack.com/p/policies-277`
  (Reached via bio.site → Substack "Policies" post; substack URLs 302-redirect from `open.substack.com/pub/...` to `<pub>.substack.com/...` — refetch the redirect target if WebFetch reports a redirect instead of content.)

## Process

1. WebFetch each source above with a prompt asking for exact prices / policy wording verbatim (not paraphrased) — vague prompts tend to get summarized into "no pricing shown."
2. Read the current `businesses/glam-by-dang/context.md`, the cancellation-tier rule in `businesses/glam-by-dang/rules.md`, and `cancellation_tiers` in `businesses/glam-by-dang/business.json`.
3. Diff live vs. current, service by service and policy tier by policy tier. Call out:
   - Numeric price changes
   - "Flat" vs. "from/starting at" framing changes (this changes how Mia is allowed to phrase a price)
   - New services not yet in `context.md`
   - Policy tier/threshold changes (e.g. day/hour cutoffs, refund vs. credit vs. nothing, deposit-only vs. full-appointment-value charges)
4. Report the diff and proposed edits — do not edit Kha's business folder until the user confirms, since pricing/policy mistakes go straight to real customers.
5. Once confirmed, apply edits in `businesses/glam-by-dang/` (`context.md` for the factual snapshot; `rules.md` and `business.json` `cancellation_tiers` together if the cancellation policy changes, since the rule text and the per-day status lines must match), then add/update a corresponding case in `tests/run_tests.py` and run it (`python3 tests/run_tests.py`) to confirm the new wording/logic actually surfaces in a reply.
