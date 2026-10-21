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
- `MASTODON_ACCESS_TOKEN` — access token of the "Happy Foods push-bots"
  application on the fleet's mastodon.social account (added 2026-10-21)

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

## Mastodon mirror (added 2026-10-21, Dee's order)

- Every feature/guide/roundup post and every Substack amplification
  is also posted to the fleet's Mastodon account on mastodon.social.
  Pure stdlib (form-encoded statuses, /api/v2/media photo uploads).
- **Dee's Mastodon rule: every Mastodon bot post carries all three
  websites** — churnmilk.com, overpepper.com, gravytoast.com — plus
  the Happy Foods Substack blog (happyfoods.substack.com), in a
  footer (`MASTO_SITES_FOOTER` in bot.py); new Substack posts are
  amplified to Mastodon by the watcher too. Mastodon ids are tracked
  per site in `state/<site>.json` under `mastodon.posted` so a
  mirror never duplicates; a failed mirror logs loudly and never
  blocks the Bluesky post. The reader stays Bluesky-only.
- Requires the `MASTODON_ACCESS_TOKEN` repo secret; `MASTODON_BASE`
  is set to https://mastodon.social in the posting workflows.

## Nostr mirror (added 2026-10-21, Dee's order)

- Every feature/guide/roundup post and every Substack amplification
  is also published as a kind-1 note to public Nostr relays from the
  fleet's own Nostr identity, **@happyfoods**
  (npub1j6ywegxsx33k7x5wqe9fhmdcma5g0nphwwhjqwkrtj4qam2p24ls6z48n8).
  Nostr has no email/password accounts — the identity is a keypair;
  the private key lives only in the `NOSTR_PRIVATE_KEY` repo secret,
  like the other fleet credentials.
- **Dee's footer rule applies exactly as on Mastodon**: every bot
  post carries churnmilk.com, overpepper.com, gravytoast.com plus
  the Happy Foods Substack blog; a feature item's photo rides as its
  URL (Nostr clients render bare image URLs inline). Event ids are
  tracked per site in `state/<site>.json` under `nostr.posted` so a
  mirror never duplicates; a failed mirror logs loudly and never
  blocks the Bluesky post. The reader stays Bluesky-only.
- Pure stdlib: BIP-340 Schnorr signing and a minimal WebSocket
  client (with HTTP CONNECT proxy support) live in bot.py — the
  workflows need no extra packages. Default relays are in
  `NOSTR_RELAYS` (damus, nos.lol, nostr.band, primal); a publish
  counts as successful when at least one relay accepts the event.
