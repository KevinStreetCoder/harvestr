# Harvestr

> **One username in → every video they've ever posted, across 50+ sites out.**

Harvestr is a cross-platform video archival tool that probes dozens of video
hosting, cam-archive, and creator-economy sites for a single username, then
pulls down every video it finds — with a browser UI, an aggressive
downloader stack (aria2c + ffmpeg + curl fallback), content-based
deduplication, and an extensible custom-scraper framework for sites
`yt-dlp` doesn't cover.

---

## Why

If you follow a creator and want a local archive of their work, their content
is usually scattered across 5-15 different sites: main platform,
cross-posted mirrors, archive sites, fan sites, leak sites, etc. Chasing
each site manually is tedious and you inevitably miss things, duplicate
downloads, or fall behind.

Harvestr solves this with one command:

```powershell
python universal_downloader.py alice_example
```

which fans out to 50+ sites in parallel, finds every profile/page for that
name, and downloads every video — skipping anything it already has.

## Features

| Capability | How |
|---|---|
| **1800+ sites via yt-dlp** | All mainstream + adult tube sites |
| **Custom scrapers for 25+ cam-archive + creator sites** | KVS mirror family, Coomer, Kemono, RedGifs, X.com, Reddit, Archivebate, Recordbate, Recu.me, CamCaps… |
| **Parallel probing** | 8-way concurrent site probing (~15 seconds for 50 sites) |
| **aria2c 16-connection downloads** | Multi-segment MP4 downloads at wire speed |
| **HLS / DASH / m3u8** | ffmpeg pipeline for fragmented streams |
| **Cloudflare bypass** | `curl_cffi` Chrome TLS fingerprint + `cloudscraper` fallback |
| **DDoS-Guard bypass** | `Accept: text/css` trick for Coomer / Kemono |
| **Cross-mirror dedup** | One video across 5 mirrors → downloaded once |
| **Content-based dedup** | Post-hoc sweep using size + head/tail SHA1 (99%+ accuracy, <50 ms per file) |
| **Cookie auth** | Netscape `cookies.txt` with per-site domain filtering |
| **Premium X.com (Twitter)** | GraphQL API with auth_token + ct0 cookies |
| **Web UI** | Flask dashboard with live log, start/stop, inline video preview |
| **Atomic state** | Thread-safe `history.json` / `failed.json`, Windows-safe |
| **Resumable** | Re-runs only download new videos; rolling window per site |
| **Dry-run mode** | See what would be downloaded without touching disk |

## Supported sites (partial list)

### Mainstream
YouTube · Dailymotion · Vimeo · Rumble · Twitch (VODs & clips) · Kick · Odysee ·
BitChute · Soundcloud · Reddit · **X.com / Twitter** (premium) · RedGifs

### Adult tubes (via yt-dlp)
PornHub · XVideos · xHamster · SpankBang · XNXX · YouPorn · Redtube ·
SpankWire · RedTube · 4Tube · TNA Flix · EPorner · Beeg · DrTuber · HotMovs ·
KeezMovies · ManyVids · Motherless · SxyPrn · Tube8

### Cam archive sites (custom scrapers)
camwhores.tv · camwhores.video · camwhores.co · camwhores.bz · camwhoresHD ·
camwhoresbay · camwhorescloud · camvideos.tv · camhub.cc · camwh.com · cambro.tv ·
camcaps.tv · camcaps.io · camstreams.tv · porntrex · camsrip · recordbate ·
archivebate · recu.me

### Creator / leak mirrors (no subscription needed)
- **Coomer.st** — OnlyFans / Fansly / CandFans mirror
- **Kemono.cr** — Patreon / Fanbox / Gumroad / SubscribeStar / Fantia / Boosty / Discord / DLSite mirror
- **RedGifs** — v2 API, auto-acquired temp token

### Full list
```powershell
python universal_downloader.py --list-sites
```

---

## Install

### Dependencies

```powershell
# Required
pip install -U "yt-dlp[default,curl-cffi]" requests cloudscraper rich flask

# Recommended (16x faster downloads)
winget install aria2.aria2

# Required for HLS / m3u8 streams
# Download from https://www.gyan.dev/ffmpeg/builds/ and add to PATH
```

### Clone

```powershell
git clone https://github.com/KevinStreetCoder/harvestr.git
cd harvestr
cp config.example.json config.json
```

---

## Quick start

### Web UI (recommended)

```powershell
python webui.py --port 7860
```

Open **http://127.0.0.1:7860** and you get:

- Performer management (add/remove by name)
- Per-site checkbox filter (or presets: **All** / **Custom only** / **yt-dlp only**)
- **Start** / **Stop** buttons with live log tail
- History table with inline video preview
- Failed / skipped table with reason codes
- One-click **Dedup** (content-based dupe scan)
- Auto-refresh every 2 seconds

### CLI

```powershell
# Download every video for one username across all sites
python universal_downloader.py alice_example

# Restrict to specific sites
python universal_downloader.py alice_example --sites coomer,kemono,xcom

# Dry-run (probe + enumerate, no downloads)
python universal_downloader.py alice_example --dry-run

# Run for every performer configured in config.json
python universal_downloader.py --all

# Show every supported site
python universal_downloader.py --list-sites

# Verbose / debug mode
python universal_downloader.py alice_example -v
```

### Content-based deduplication

```powershell
python dedupe.py            # scan & report, no changes
python dedupe.py --apply    # actually delete dupes
python dedupe.py --performer alice_example   # limit to one
```

Dedup uses size + 64 KB head SHA1 + 64 KB tail SHA1, catching >99% of
real duplicates in <50 ms per file. Keeper chosen by longest filename
(most descriptive title), tiebreaker oldest mtime.

---

## Config

### `config.json`

```json
{
  "output_dir": "C:\\...\\downloads",
  "performers": ["alice_example", "bob_example"],
  "enabled_sites": [],
  "max_videos_per_site": 200,
  "min_probe_entries": 1,
  "max_parallel_probes": 8,
  "max_parallel_downloads": 3,
  "min_disk_gb": 5.0,
  "use_aria2c": true,
  "aria2c_connections": 16,
  "rate_limit": "",
  "cookies_from_browser": "",
  "cookies_file": "",
  "impersonate_target": "chrome",
  "min_duration_seconds": 30.0,
  "retries": 5,
  "probe_timeout": 60,
  "verbose": false
}
```

| Field | Purpose |
|---|---|
| `performers` | List used by `--all` and by the UI |
| `enabled_sites` | Empty = all sites. Otherwise a whitelist |
| `max_videos_per_site` | Rolling-window cap per performer per site per run |
| `max_parallel_probes` | How many site probes run concurrently |
| `max_parallel_downloads` | How many videos download concurrently |
| `min_disk_gb` | Pause if free space drops below this |
| `use_aria2c` | Toggle aria2c multi-segment downloader |
| `aria2c_connections` | Connections per file (16 = sweet spot) |
| `rate_limit` | Per-download cap, e.g. `"500K"` / `"2M"` |
| `cookies_from_browser` | `"chrome"` / `"firefox"` — picks up login cookies |
| `cookies_file` | Path to Netscape cookies.txt |
| `impersonate_target` | curl_cffi target, `"chrome"` is safe default |
| `min_duration_seconds` | Skip very short clips |

### Cookies

Some sites (recu.me, camwhores.tv private videos, camvault, X.com premium)
require login cookies. See **[COOKIES_SETUP.md](COOKIES_SETUP.md)** for the
full cookie-export walkthrough.

Sites that **do not** need auth:
Coomer, Kemono, RedGifs, Reddit (public), all KVS mirrors (tags/search pages).

Sites that benefit from auth:
X.com (premium = 10× daily quota), Recu.me (premium = unlimited plays).

Sites that absolutely need auth:
camwhores.tv "friend-locked" private videos, Recurbate premium downloads.

---

## Architecture

```
┌─────────┐     ┌──────────────────┐     ┌─────────────────────┐
│   CLI   │ --> │  UniversalDown-  │ --> │  probe_all_sites    │
│   or    │     │     loader       │     │  (parallel fanout)  │
│   UI    │     │  (orchestrator)  │     └─────┬───────────────┘
└─────────┘     └──────────────────┘           │
                                                │
        ┌───────────────────────────────────────┴──────────┐
        │                                                   │
        ▼                                                   ▼
┌────────────────┐                                 ┌─────────────────┐
│  yt-dlp flat   │                                 │ custom scrapers │
│ extraction (29 │                                 │  (25+ classes)  │
│  sites cfg'd)  │                                 │ Coomer, Kemono, │
└───────┬────────┘                                 │ KVS family, ... │
        │                                          └────────┬────────┘
        └─────────────┬────────────────────────────────────┘
                      ▼
              ┌────────────────┐
              │   filter_new   │  <-- cross-mirror dedup by video_id
              │  (history +    │  <-- URL / title filter (Macy Cartel etc.)
              │   failed.json) │
              └───────┬────────┘
                      ▼
              ┌────────────────────────────┐
              │   download_videos          │
              │   ┌──────────────────────┐ │
              │   │ aria2c (MP4)         │ │
              │   │ ffmpeg (HLS/DASH)    │ │
              │   │ curl fallback        │ │
              │   └──────────────────────┘ │
              └───────┬────────────────────┘
                      ▼
              ┌────────────────┐
              │ atomic history │
              │ write + lock   │
              └────────────────┘
```

### Custom scraper contract

Every scraper in `custom_scrapers.py` implements four methods:

```python
class MyScraper(SiteScraper):
    NAME = "mysite"
    BASE_URL = "https://mysite.com"
    CATEGORY = "adult"               # or "mainstream", "archive"
    COOKIE_DOMAIN = "mysite.com"     # optional — filter cookies.txt per site

    def probe(self, username) -> Optional[ProbeHit]:
        """Cheap test: does this user exist here? Return None or a hit."""

    def enumerate(self, hit, username, limit) -> List[VideoRef]:
        """List all video refs (may or may not populate stream URL)."""

    def extract_stream(self, ref) -> bool:
        """Resolve a ref's playable URL (m3u8 / direct mp4). Returns True on success."""
```

Register the class in `ALL_SCRAPER_CLASSES` at the bottom of
`custom_scrapers.py` and it's auto-picked-up by both CLI and UI.

### State files

Everything under `downloads/`:
- `history.json` — successful downloads, keyed by `{performer: {site|video_id: info}}`
- `failed.json` — failures, marked permanent after 3 attempts if dead / private
- `universal.log` — full debug log (also tailed live in the UI)

---

## Testing

Live end-to-end smoke test for the new scrapers (actually downloads one
small clip per working scraper):

```powershell
python tests/smoketest_new_scrapers.py
```

Expected output:
```
[PASS]  Coomer (OnlyFans/Fansly mirror)      PIPELINE OK (download skipped: CDN unreachable from this network)
[PASS]  Kemono (Patreon/Fanbox mirror)       PIPELINE OK (download skipped: CDN unreachable from this network)
[PASS]  RedGifs                              OK  user=toasted500  3.51 MB -> ...
[PASS]  Reddit user                          OK  user=GallowBoob  9.06 MB -> ...
[FAIL]  X.com (needs auth cookies)           (expected: no cookies.txt)
```

Coomer/Kemono produce valid URLs but their CDN shards (`n1-n4.coomer.st` /
equivalents) are blocked by some ISPs — use a VPN if the actual download step
times out.

---

## Research & reference

See **[research/platforms_research.md](research/platforms_research.md)** for
a deep dive on each platform's auth model, API quirks, rate limits, and the
recommended integration approach.

---

## Legal & ethics

This tool is for **archiving content you have a right to access**:
creators you subscribe to, content in the public domain, content under
permissive licenses, backups of your own uploads, etc.

Don't use it to:
- Redistribute copyrighted content
- Bypass paywalls for content you don't have a legitimate license to
- Scrape at a rate that abuses or disrupts a host site
- Circumvent technological protection measures that violate your local
  jurisdiction's anti-circumvention laws

You are responsible for complying with each site's Terms of Service and
your local law. The authors disclaim any liability for misuse.

---

## License

MIT — see [LICENSE](LICENSE).

## Credits

Stands on the shoulders of:
- [**yt-dlp**](https://github.com/yt-dlp/yt-dlp) — the universal extractor
- [**aria2**](https://aria2.github.io/) — multi-segment downloads
- [**ffmpeg**](https://ffmpeg.org/) — HLS / DASH demuxing
- [**curl_cffi**](https://github.com/lexiforest/curl_cffi) — Chrome TLS fingerprint
- [**cloudscraper**](https://github.com/VeNoMouS/cloudscraper) — Cloudflare IUAM bypass
- [**Flask**](https://flask.palletsprojects.com/) — web UI
