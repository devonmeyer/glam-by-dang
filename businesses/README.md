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

## Connecting a business's Instagram

Owners connect their own Instagram through Instagram's login screen; nobody pastes tokens.

1. Create the business's folder (above) and deploy it.
2. Make a one-time link (valid 7 days):
   ```
   curl -X POST https://itsmia.info/admin/invites \
     -H "X-Admin-Secret: $ADMIN_SECRET" -H "Content-Type: application/json" \
     -d '{"slug": "<folder name>"}'
   ```
3. Send the owner the `url` it returns. They log in with the business's Instagram (a professional account, with "Allow access to messages" on) and approve.
4. Mia saves the token encrypted, refreshes it on its own, and starts receiving that account's DMs.

Until Meta app review passes, the owner's Instagram must first be added as an Instagram tester on the Meta app and accept the invite.

One-time Meta app dashboard settings (Instagram > API setup with Instagram login):
- OAuth redirect URI: `https://itsmia.info/auth/instagram/callback`
- Deauthorize callback URL: `https://itsmia.info/auth/instagram/deauthorize`
- Data deletion request URL: `https://itsmia.info/auth/instagram/delete`

## Admin commands

All need the `X-Admin-Secret` header. Leave out `?slug=` to use Kha's business.

```
# See what a business's summary would say (doesn't send or clear anything)
curl "https://itsmia.info/preview-summary?slug=<slug>" -H "X-Admin-Secret: $ADMIN_SECRET"

# Send a business's summary now (clears the conversations it covers)
curl -X POST "https://itsmia.info/trigger-summary?slug=<slug>" -H "X-Admin-Secret: $ADMIN_SECRET"

# An owner asked directly for their data to be deleted: disconnects their Instagram
# and deletes their stored conversation summaries
curl -X POST https://itsmia.info/admin/delete-data \
  -H "X-Admin-Secret: $ADMIN_SECRET" -H "Content-Type: application/json" \
  -d '{"slug": "<slug>"}'
```

Railway variables: `INSTAGRAM_APP_ID`, `PUBLIC_BASE_URL`, `ADMIN_SECRET`, `TOKEN_ENCRYPTION_KEY` (see `.env.example`), plus the existing `META_APP_SECRET`.
