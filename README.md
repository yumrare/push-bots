# push-bots — Bluesky fleet for ChurnMilk, GravyToast, Overpepper

One Bluesky account — **@happyfoods.bsky.social** (Dee's umbrella curator
account, created 2026-10-19) — pushes the three quiet sites. Pure-stdlib
Python (AT Protocol over urllib), modeled on the yumrare-bots reliability
design: per-site state files, an item is marked posted only after its post
succeeds, rotations never duplicate.

## Bots (13)
Per site (churnmilk / gravytoast / overpepper):
- **Feature poster** — daily; next catalog item in rotation, photo when
  the entry has one, link card when it doesn't.
- **Guide poster** — Mon/Wed/Fri; rotates the site's guide/chart pages.
- **Roundup poster** — Sundays; "This week on <Brand>" from the week's
  feature history (skips if fewer than 2).
- **Reader** — daily; keyword search in the site's niche, capped at
  8 likes + 3 follows per day. No comments.

Plus the **fleet keeper** — daily REPORT.md health check (flags any site
whose last feature is >48h old).

## Schedules (GitHub Actions, UTC)
- features.yml — 15:05 daily (~10:05 AM CT)
- guides.yml — 17:10 Mon/Wed/Fri (~12:10 PM CT)
- engage.yml — 19:20 daily (~2:20 PM CT)
- roundup.yml — 16:15 Sundays (~11:15 AM CT)
- keeper.yml — 20:35 daily (~3:35 PM CT)

## Secrets
- `BSKY_HANDLE` — happyfoods.bsky.social
- `BSKY_APP_PASSWORD` — the account's app password

## Refreshing a catalog
Edit a site's data at its source, then run `python3 prep_sites.py` here
(it rebuilds sites/*.json from the site workspaces) and commit.

## Dry run
`DRY_RUN=1 python3 bot.py feature --site churnmilk` — composes and logs,
posts nothing, touches no state. Missing secrets also force dry-run.

## Tumblr mirror + Substack watcher (added 2026-10-20, Dee's order)

- **Tumblr mirror** — every feature/guide/roundup post is also posted to
  the fleet's Tumblr blog **@happyfoods1** ("Happy Foods") via the legacy
  v2 API (OAuth1, same pattern as yumrare-bots). Photo posts use the
  entry's photo by source URL; guides/roundups/Substack amplifications
  are link/text posts. Mirror ids are tracked per site in
  `state/<site>.json` under `tumblr.posted` so nothing ever duplicates;
  a failed mirror logs loudly and never blocks the Bluesky post. The
  reader stays Bluesky-only. Requires the `TUMBLR_*` repo secrets and
  `requests` + `requests-oauthlib` (installed in the posting workflows).
- **Substack watcher** (`bot.py substack`, substack.yml, hourly) —
  reads https://happyfoods.substack.com/feed; any NEW post on Dee's
  Happy Foods Substack is amplified to the fleet's Bluesky + Tumblr.
  First run seeds existing posts WITHOUT posting (no back-catalog
  blast). State: `state/substack.json`.
