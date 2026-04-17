# Cookie-based authentication for Universal Downloader

Some sites require login cookies to access their content:

| Site | Free | With cookies |
|---|---|---|
| **Recu.me / Recurbate** | Blocked (Cloudflare) | Works (free: 1-5 plays/day; premium: unlimited) |
| **camwhores.tv private videos** | Skipped (PRIVATE msg) | Works if you're a "friend" of the uploader |
| **camvault.to** | Preview only | Full downloads with premium |
| **Recurbate.pro** | Preview only | Full downloads with premium |
| **X.com / Twitter** | 1 k posts/day | Premium = 10 k posts/day, longer videos, full archive |
| **Coomer.st** (OF / Fansly mirror) | Works — no auth needed | n/a |
| **Kemono.cr** (Patreon / Fanbox mirror) | Works — no auth needed | n/a |
| **RedGifs** | Works — auto-temp-token | n/a |
| **Reddit user posts** | Works (public) | Increases rate limit + NSFW posts |

## How to export cookies

### Chrome / Edge / Brave

1. Install the **"Get cookies.txt LOCALLY"** extension (verified, no-tracking version):
   - https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc
2. Log in to the target site normally (e.g., https://recu.me)
3. Click the extension icon
4. Click "Export" → saves `cookies.txt`

### Firefox

1. Install the **"cookies.txt"** extension:
   - https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/
2. Log in to the target site
3. Click the extension icon → "Current Site" → saves `cookies.txt`

### Manual (yt-dlp method)

yt-dlp can read cookies directly from your installed browser:
```powershell
yt-dlp --cookies-from-browser chrome --print-to-file "" -o "cookies.txt" https://recu.me/
```
(This doesn't print anything, but populates `cookies.txt` with the browser's cookie jar.)

## Configuration

Save the exported file somewhere, e.g.:
```
C:\Users\Street Coder\Documents\Scripts\Downloaders\universal\cookies.txt
```

Then edit `config.json`:
```json
{
  ...
  "cookies_file": "C:\\Users\\Street Coder\\Documents\\Scripts\\Downloaders\\universal\\cookies.txt",
  ...
}
```

The script will automatically filter cookies per site — you can put cookies for
**multiple sites in the same file** (recu.me + camwhores.tv + archivebate.com)
and each scraper picks the ones matching its `COOKIE_DOMAIN`.

## Recu.me specifics

### Free account (1-5 plays/day)
1. Sign up at https://recu.me/account/signup (email only, no payment)
2. Log in in your browser
3. Export cookies as above
4. The script can now download ~5 videos/day before hitting the quota

### Premium account (unlimited + downloads)
1. Buy a premium subscription at https://recu.me/account/subscribe
2. Log in, export cookies
3. Unlimited plays, no "shall_subscribe" errors, official download links

### Required cookies for Recu.me
The script specifically looks for:
- `cf_clearance` — Cloudflare anti-bot token (mandatory, 30-60 min TTL)
- `im18` — age gate (trivial, just `true`)
- `PHPSESSID` or similar — your login session (needed for premium access)

If `cf_clearance` expires mid-run, the next probe will fail — just re-export
cookies from your browser.

## camwhores.tv private videos

Private videos on camwhores.tv say "This video is a private video uploaded by X.
Only members who upload videos can watch private videos."

To download them:
1. Create a camwhores.tv account (you need to have uploaded at least 1 video
   yourself to become a "member")
2. Add the uploader as a friend (requires their acceptance)
3. Export cookies from the authenticated browser session
4. Place cookies in `cookies.txt`, point `config.json` at it

The `KVSScraper._is_private` check in the code already uses authenticated cookies
when present — with the right session it can access friend-locked videos.

## What if cookies stop working?

- **cf_clearance expired**: Re-export from browser (quickest)
- **PHPSESSID invalid**: Re-login to the site in your browser, re-export
- **Site changed auth scheme**: Check the per-site scraper code in
  `custom_scrapers.py` — search for `COOKIE_DOMAIN` to see what's expected

## Security notes

- `cookies.txt` gives anyone who has it full access to your accounts on those
  sites. Keep it out of git, don't share, don't email.
- The `universal_downloader.py` only READS the file, never writes to it.
- Cookies are loaded in-memory per-run; no plaintext caching.

## X.com (Twitter) — premium gives 10× the daily budget

The X scraper uses the private GraphQL API (`/i/api/graphql/...`). Without cookies
it falls back to gallery-dl/yt-dlp (slow and rate-limited). With premium cookies
it pulls the full user-media timeline directly.

### What to export

Cookies the scraper actually reads:

| Cookie | Why |
|---|---|
| `auth_token` | Your login session — **required** |
| `ct0` | CSRF token — **required**, sent as `x-csrf-token` header |
| `guest_id`, `personalization_id`, `twid` | Optional, but recommended for fewer challenges |

### How to export

1. Log into https://x.com in Chrome/Firefox with your **premium** account
2. Use the "Get cookies.txt LOCALLY" extension → click Export on x.com
3. Append the exported file to your existing `cookies.txt` (or save as `cookies_x.txt`
   and point `cookies_file` at it — the scraper filters per domain)

### Sanity check

```powershell
python universal_downloader.py yourXusername --sites xcom --dry-run
```

Look for: `HIT: xcom (N videos) @ https://x.com/i/user/...`. If you see
`no auth_token cookie — scraper skipped`, re-export.

### Limits

- Free tier: ~1 k UserTweets calls/day/IP
- Premium: ~10 k UserTweets calls/day
- Platform-wide ceiling: 3200 tweets per user (regardless of tier)
- Long videos (>2 min) require premium

## Coomer.st / Kemono.cr — NO auth needed

Both mirror paid-platform content (OnlyFans, Fansly, Patreon, Fanbox, Gumroad,
SubscribeStar, Discord, etc.). Scrapers use the public `/api/v1/...` JSON.

The only trick: these sites sit behind DDoS-Guard, which rejects the default
`Accept: text/html`. The scrapers automatically send `Accept: text/css`, which
sails through. **You do not need cookies.**

```powershell
python universal_downloader.py blondie_254 --sites coomer --dry-run
# HIT: coomer (326 videos) @ https://coomer.st/onlyfans/user/blondie_254
```

Same for `kemono` — tests with `GumroadCreator/Patreon` work out of the box.

### When does Coomer need subscriber cookies?

Some creators on Coomer are locked behind their own site subscription (rare).
The scraper will skip those. Adding an OnlyFans session cookie here does NOT
help — Coomer re-hosts, it doesn't proxy.

## RedGifs — automatic temp token

The scraper hits `https://api.redgifs.com/v2/auth/temporary` on every run to get
a 24-hour JWT. No user action needed. If the rate-limit becomes a problem, log
into redgifs.com and export cookies to `cookies.txt` — they raise the per-IP cap.

## Reddit — optional auth for NSFW + higher rate limit

The scraper reads `/user/{name}/submitted.json` — public endpoint, works unauth'd
but gets hit by Reddit's 10 req/min ceiling.

### For NSFW / 18+ user posts

1. Log into Reddit, opt in to NSFW in account settings
2. Export cookies — the scraper sends the `reddit_session` cookie automatically
3. NSFW submissions now appear in the feed

If you see 403s from `www.reddit.com`, that's typically the (fixed) `Accept: text/html`
bug — recent version sends `Accept: application/json`. Upgrade `custom_scrapers.py` if
you see this on an old copy.

## OnlyFans / Fansly / Patreon (direct) — NOT recommended

Scrapers for these are **intentionally not built in** because:

- **OnlyFans**: Needs rotating request-signing rules from DATAHOARDERS/dynamic-rules,
  `x-bc` device fingerprint, paid session cookies (`sess`, `auth_id`, `fp`), and
  Widevine L3 CDM (`device.wvd`) for DRM videos. High-maintenance.
- **Fansly**: Simpler — just an `authorization` token from localStorage — but still
  requires a paid sub per creator.
- **Patreon**: `session_id` cookie + paid tier per campaign. Some content is also DRM.

**Use coomer.st / kemono.cr instead** — they cover 95%+ of public creators for free.
If you *must* hit the source, use a maintained upstream tool:

- `ultima-scraper` (for OnlyFans)
- `gallery-dl` (supports Patreon via cookies)

See `research/platforms_research.md` for the full teardown.

## Web UI

Instead of editing `config.json` and running CLI flags, launch the web UI:

```powershell
python webui.py --port 7860
# → open http://127.0.0.1:7860
```

Features:
- Add / remove performers by name
- Toggle any of the 53 sites (Coomer, X.com, Kemono, RedGifs, cam mirrors, …)
  — click "All" / "Custom only" / "yt-dlp only" quick presets
- Start / stop downloads, run on a single performer
- Live log tail (auto-scrolls)
- History table (size, site, date) with play-in-browser button
- Failed/skipped table with the reason code
- One-click dedup
- Stat counters (performer count, total downloaded, disk usage)

The UI runs the exact same `universal_downloader.py` subprocess — nothing is
re-implemented, so every scraper you add via `custom_scrapers.py` shows up
automatically.

## Test your cookies

```powershell
# Dry-run only (no downloads) to check which sites accept your cookies:
python universal_downloader.py --all --dry-run
```

Look for log lines like:
- `[recume] loaded 12 cookies for domain recu.me` — good, recu.me cookies present
- `[recume] no cf_clearance cookie — skipping` — bad, export again after solving
  the Cloudflare challenge in your browser
- `HIT: recume (N videos) @ https://recu.me/performer/USERNAME` — working!
- `HIT: xcom (42 videos) @ https://x.com/i/user/...` — X.com premium cookies accepted
- `HIT: coomer (N videos) @ https://coomer.st/onlyfans/user/...` — Coomer hit (no cookies needed)
