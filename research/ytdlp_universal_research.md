# Universal Video Downloader — Research Report (yt-dlp based)

_Research conducted 2026-04-16. Results may decay as sites change._

This report informed the design of the `universal_downloader.py` in the parent folder.
The design principle is: **use yt-dlp's 1800+ built-in extractors rather than writing
site-specific anti-bot code**. yt-dlp is maintained upstream so site changes are
handled by their community, not by us.

---

## 1. yt-dlp as a Python library

### YoutubeDL class — basic usage

```python
import yt_dlp

ydl_opts = {
    'quiet': True,
    'no_warnings': True,
    'skip_download': True,           # metadata only
    'extract_flat': 'in_playlist',   # lazy — don't resolve individual videos
    'ignoreerrors': True,            # keep going when one entry fails
    'outtmpl': '%(uploader)s/%(title)s [%(id)s].%(ext)s',
    'http_headers': {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://example.com/',
    },
    'cookiefile': '/path/to/cookies.txt',
    # or: 'cookiesfrombrowser': ('chrome', None, None, None),
}

with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    info = ydl.extract_info(url, download=False)
    info = ydl.sanitize_info(info)   # JSON-safe version
```

Key points:
- `extract_info(url, download=False)` works on both single-video URLs and playlist/channel/search URLs.
- Return dict has `_type: 'video'` for single items or `_type: 'playlist'` with `entries` for lists.
- `sanitize_info()` is required if you want JSON-serializable output.
- `extract_flat='in_playlist'` returns bare entries without resolving each one — essential for fast enumeration.

### extract_info on user/tag/search URLs

yt-dlp treats any URL the same: it looks up the matching extractor by regex, then returns either a video dict or a `playlist`-type dict with `entries`. So feeding it `https://www.pornhub.com/model/katekuray` returns a playlist-type dict containing every video the model-page extractor can find. Each `entry` is then passed to the single-video extractor on demand.

### Site-specific search mode

Search pseudo-URLs use the prefix pattern `<key><N>:<query>`:
- `ytsearch5:username` (YouTube, 5 results)
- `ytsearchdate:username` (sorted by date)
- `ytmsearch:` (YouTube Music), `scsearch:` (SoundCloud), `yvsearch:` (Yahoo Video)
- Many site extractors also handle their native search pages as regular URLs.
- There's no universal `<anySite>search:` prefix — only sites whose extractor registers a `SearchInfoExtractor` get one.

### match_filter and playlist handling

```python
from yt_dlp.utils import match_filter_func

ydl_opts.update({
    'playlistend': 50,                       # first 50 entries
    'playlist_items': '1-20,30,40-50',        # index spec
    'lazy_playlist': True,                   # stream entries as received
    'match_filter': match_filter_func(
        '!is_live & duration > 60 & view_count > 1000'
    ),
})
```
- Filter syntax supports `&` (AND), `~=` (regex), `!` (not), `?` (optional field).
- `lazy_playlist` lets you start processing without waiting for the whole enumeration — important for large model pages.

### aria2c integration

```python
ydl_opts.update({
    'external_downloader': {
        'default': 'aria2c',
        'm3u8': 'native',           # let yt-dlp handle HLS fragments natively
        'dash': 'native',
    },
    'external_downloader_args': {
        'aria2c': [
            '-x', '16',              # 16 connections per file
            '-j', '4',               # 4 simultaneous downloads
            '-s', '16', '-k', '1M',
            '--console-log-level=warn',
            '--summary-interval=0',
        ],
    },
})
```
HLS/DASH via aria2c has known resume (`-c`) issues — the native downloader with `-N <n>` concurrent fragments is usually more reliable for adult/cam sites that use HLS.

### Cookies / UA / Referer

- `cookiefile` — path to Netscape-format cookies.txt
- `cookiesfrombrowser` — tuple `(browser, profile, keyring, container)`
- `http_headers` — dict; set `User-Agent`, `Referer`, `Accept-Language`
- Per-request override: pass `http_headers` in individual `ydl_opts`

### Listing extractors in Python

```python
import yt_dlp
all_ext = list(yt_dlp.list_extractor_classes())       # every class
working = [ie for ie in yt_dlp.gen_extractor_classes() if ie.working()]
pornhub = yt_dlp.get_info_extractor('Pornhub')        # by name
# Each class has IE_NAME, _VALID_URL (regex), and SUITABLE_URLS
```

CLI equivalents: `yt-dlp --list-extractors` and `yt-dlp --extractor-descriptions`.

---

## 2. Sites with performer/username/tag pages supported by yt-dlp

### Mainstream / general video hosts

| Site | yt-dlp support | User URL pattern | Search prefix | Free? |
|---|---|---|---|---|
| YouTube | Excellent | `/@USER`, `/@USER/videos`, `/@USER/shorts`, `/c/NAME`, `/user/NAME`, `/channel/ID` | `ytsearch:`, `ytsearchdate:`, `ytmsearch:` | Yes |
| Vimeo | Yes | `/NAME`, `/user123456/videos` | parse results | Yes |
| Twitch | Yes | `/NAME/videos`, `/NAME/clips` | no prefix | Yes |
| Kick | Yes (official) | `/NAME` | — | Yes |
| Rumble | Yes | `/c/NAME`, `/user/NAME` | — | Yes |
| Dailymotion | Yes | `/USERNAME` | no prefix | Yes |
| BitChute | Yes | `/channel/NAME/` | `/search?query=Q` | Yes |
| Odysee | Yes | `/@NAME`, `/@NAME:c` | — | Yes |
| Internet Archive | Yes (`archive.org`) | `/details/@USER`, `/details/IDENTIFIER` | `archive.org/search.php?query=Q` | Yes |
| SoundCloud | Yes | `/USERNAME` | `scsearch:` | Yes |

### Adult tube sites (confirmed extractors)

| Site | yt-dlp support | User/Performer URL pattern | Notes |
|---|---|---|---|
| Pornhub | Yes — `Pornhub`, `PornhubUser`, `PornhubPagedVideoList`, `PornhubUserVideosUpload` | `/model/NAME`, `/pornstar/NAME`, `/users/NAME`, `/channels/NAME` | — |
| XVideos | Yes — `XVideos` + profiles/channels/search/favorites | `/profiles/NAME`, `/channels/NAME` | — |
| XHamster | Yes — `XHamster`, `XHamsterUser`, `XHamsterEmbed` | `/users/NAME/videos` | Flaky Q1 2026 |
| RedTube | Yes — single videos only; no user extractor | n/a | — |
| YouPorn | Yes — extractor exists but endpoint broken as of 2025/2026 | `/uservids/NAME` | — |
| Tube8 | Yes — `Tube8IE` | `/profile/NAME/videos/` | Degraded |
| Eporner | Yes — extractor exists, degraded | `/profile/NAME/` | Hash extraction failing |
| SpankBang | Yes — `SpankBang`, `SpankBangPlaylist` | `/profile/NAME/videos`, `/NAME/playlists` | **Needs `curl_cffi` impersonation** |
| Motherless | Yes — `Motherless`, `MotherlessGroup` | `/m/NAME` (member), `/g/NAME` (group) | — |
| ManyVids | Yes — extractor exists | `/Profile/NAME/Store/Videos/` | Most paid |
| 4tube | Yes | `/channels/NAME/videos`, `/pornstars/NAME` | — |
| Beeg | Yes | tag-based | — |
| DrTuber | Yes | `/user/NAME` | — |
| KeezMovies | Yes | `/pornstars/NAME` | — |
| Spankwire | Yes | `/user/NAME/videos/` | — |
| TNAFlix | Yes | `/profile/NAME` | — |
| XNXX | Yes | `/pornstar/NAME`, `/profiles/NAME` | — |
| YourPorn (SXYPrn) | Yes | `/user/NAME` | — |

### Cam / live-stream sites

| Site | Supported | Notes |
|---|---|---|
| Chaturbate | Yes — single live stream only; no VOD or user-past-videos | `/NAME` is the live stream |
| Stripchat | Yes (flaky — "No active stream" issues in 2026) | live only |
| CamSoda | Yes | live only |
| CAM4 | Yes | live only |
| BongaCams | Yes | live only |
| **Recu.me** | **No** — open site request (yt-dlp issue #10083) | Needs custom extractor or plugin |
| Recurbate | Yes — requires login (403 without credentials) | — |
| **CamCaps / CamVault / Archivebate** | **No** — no yt-dlp extractor | Custom scraper needed |

Key observation: **live cam sites give you only the current live stream**, not per-model archive. For historical per-model content you need the archive/recording sites, most of which do NOT have yt-dlp extractors.

---

## 3. Search strategy

### Direct URL-pattern enumeration (preferred over search)

For each site, build a list of candidate URLs from a single `username`. The downloader tries them one by one and keeps the ones that return a non-empty playlist.

```
PH:         https://www.pornhub.com/model/{u}
            https://www.pornhub.com/pornstar/{u}
            https://www.pornhub.com/users/{u}
            https://www.pornhub.com/channels/{u}
XVideos:    https://www.xvideos.com/profiles/{u}
            https://www.xvideos.com/channels/{u}
XHamster:   https://xhamster.com/users/{u}/videos
Spankbang:  https://spankbang.com/profile/{u}/videos
Motherless: https://motherless.com/m/{u}
Eporner:    https://www.eporner.com/profile/{u}/
ManyVids:   https://www.manyvids.com/Profile/{u}/Store/Videos/
TNAFlix:    https://www.tnaflix.com/profile/{u}
YourPorn:   https://sxyprn.com/{u}.html
4tube:      https://www.4tube.com/channels/{u}/videos
YouTube:    https://www.youtube.com/@{u}/videos
Vimeo:      https://vimeo.com/{u}
Twitch:     https://www.twitch.tv/{u}/videos
Rumble:     https://rumble.com/c/{u}
BitChute:   https://www.bitchute.com/channel/{u}/
Odysee:     https://odysee.com/@{u}
IA:         https://archive.org/details/@{u}
```

### Username-search fallback

For sites where the username doesn't map to a fixed URL:
1. **Native site search URLs** (scraped via the site extractor).
2. **Search-prefix URLs** on sites that expose a `SearchInfoExtractor` (YouTube, SoundCloud).

### Google dorking (cross-site discovery)

```
site:pornhub.com   "USERNAME"
site:xvideos.com   "USERNAME"
site:xhamster.com  inurl:users "USERNAME"
site:spankbang.com inurl:profile "USERNAME"
```

Programmatic options:
- `ddgs` / `duckduckgo-search` (free, rate-limited)
- SerpAPI (paid)
- Google Custom Search JSON API (free 100/day)

---

## 4. Best practices

### Rate limiting

- Per-site queue with a semaphore: cap concurrent in-flight requests to a single domain (2–4 is safe).
- Randomized delays in yt-dlp opts:
  ```python
  'sleep_interval_requests': 1,
  'sleep_interval': 2,
  'max_sleep_interval': 5,
  ```
- `ratelimit`: cap bandwidth per download when running many in parallel.
- Respect `Retry-After` headers on 429s — yt-dlp honors them automatically.

### Cloudflare bypass

1. Install `curl_cffi` and let yt-dlp auto-select an impersonation target:
   ```bash
   pip install "yt-dlp[default,curl-cffi]"
   ```
2. Fallback: browser cookies (`cookiesfrombrowser`) to use a valid `cf_clearance` cookie.
3. For stubborn sites: put a persistent `cloudscraper` or `Playwright` session in front, dump cookies, hand them to yt-dlp.

### User-Agent rotation

- Maintain a small pool of realistic desktop UAs. Rotate per-site.
- Match UA to impersonation target when using `curl_cffi`.
- Stick with one UA per `YoutubeDL` instance.

### State caching

- **Download archive**: `'download_archive': 'archive.txt'` — yt-dlp skips entries already listed.
- Persist `info_dict` JSON per video (`'writeinfojson': True`) for resumability.
- For discovery, cache the site→URL→entries mapping in SQLite keyed by `(site, username, timestamp)` with a TTL (24–72 h).

---

## 5. Python stack recommendations

| Concern | Library | Why |
|---|---|---|
| Core extraction/download | `yt-dlp` | Unrivaled site coverage |
| Cloudflare/TLS fingerprinting | `curl_cffi` | Native yt-dlp integration; JA3/JA4 impersonation |
| Concurrency | `concurrent.futures.ThreadPoolExecutor` | yt-dlp is blocking; threads are simpler |
| HTTP for discovery/scraping | `httpx` or `requests` | — |
| Search APIs | `ddgs` (free) or `serpapi` (paid) | — |
| HTML parsing | `selectolax` | Faster than bs4 |
| Browser automation fallback | `playwright` | For sites needing JS |
| Cache | `diskcache` or SQLite | k/v with TTL |
| Progress / logging | `rich` + `logging` | yt-dlp accepts custom `logger` |

---

## 6. Engine pattern

```python
from concurrent.futures import ThreadPoolExecutor, as_completed
import yt_dlp

SITE_PROBES = {
    'pornhub':  ['https://www.pornhub.com/model/{u}',
                 'https://www.pornhub.com/pornstar/{u}',
                 'https://www.pornhub.com/users/{u}'],
    'xvideos':  ['https://www.xvideos.com/profiles/{u}',
                 'https://www.xvideos.com/channels/{u}'],
}

def probe(site, url_template, username):
    url = url_template.format(u=username)
    opts = {'quiet': True, 'extract_flat': True, 'skip_download': True,
            'ignoreerrors': True, 'playlistend': 1}
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            if info and info.get('entries'):
                return site, url
        except Exception:
            return None

def find_all(username):
    jobs = [(site, tpl, username)
            for site, tpls in SITE_PROBES.items() for tpl in tpls]
    hits = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = [ex.submit(probe, *j) for j in jobs]
        for f in as_completed(futs):
            r = f.result()
            if r: hits.append(r)
    return hits
```

---

## 7. Key findings and watch-outs

- **Most cam archive sites (Recu.me, CamCaps, CamVault, SexyC, Archivebate) have no yt-dlp extractor** — you'll need custom HTTP scrapers or plugin extractors.
- **Several major extractors are partially broken in early 2026**: XHamster, YouPorn, Eporner, SpankBang (without curl_cffi), Stripchat, PornHub new-video HLS. Build your pipeline to tolerate per-site breakage and fall back to the next site.
- **PornHub model pages contaminate with watch history** — filter entries to the model's uploads only (check `uploader_id`/`uploader_url` on each entry).
- **Live cam sites only give the live stream** — they are not useful for historical content.
- **Always use `extract_flat='in_playlist'` + `lazy_playlist=True` for discovery**, then fully resolve only the videos you actually want to download. This is the single biggest perf lever.
- **Use `download_archive`** for free cross-run dedupe.
- **`curl_cffi` is load-bearing** for many adult sites in 2026 (TLS fingerprinting on Cloudflare).

---

## 8. Sources

- [yt-dlp GitHub repo](https://github.com/yt-dlp/yt-dlp)
- [yt-dlp YoutubeDL.py source](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/YoutubeDL.py)
- [yt-dlp extractors.py](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/_extractors.py)
- [yt-dlp Python API Overview](https://mintlify.wiki/yt-dlp/yt-dlp/api/overview)
- [yt-dlp Supported Sites](https://mintlify.wiki/yt-dlp/yt-dlp/reference/supported-sites)
- [yt-dlp FAQ wiki](https://github.com/yt-dlp/yt-dlp/wiki/FAQ)
- [Information Extraction Pipeline (DeepWiki)](https://deepwiki.com/yt-dlp/yt-dlp/2.2-information-extraction-pipeline)
- [External Downloader Integration (DeepWiki)](https://deepwiki.com/yt-dlp/yt-dlp/4.3-external-downloaders)
- [Downloading Playlists (yt-dlp docs)](https://mintlify.wiki/yt-dlp/yt-dlp/guides/playlists)
- [curl_cffi documentation](https://curl-cffi.readthedocs.io/en/latest/)
- [curl_cffi for Cloudflare (Datahut)](https://www.blog.datahut.co/post/web-scraping-without-getting-blocked-curl-cffi)
- [yt-dlp issue #3393 — aria2c as downloader](https://github.com/yt-dlp/yt-dlp/issues/3393)
- [yt-dlp issue #11022 — concurrent downloads](https://github.com/yt-dlp/yt-dlp/issues/11022)
- [Issue #13463 — PornHub model watch history leak](https://github.com/yt-dlp/yt-dlp/issues/13463)
- [Issue #13903 — PornHub new videos HLS](https://github.com/yt-dlp/yt-dlp/issues/13903)
- [Issue #15239 — XHamster broken](https://github.com/yt-dlp/yt-dlp/issues/15239)
- [Issue #11595 — SpankBang impersonation](https://github.com/yt-dlp/yt-dlp/issues/11595)
- [Issue #10083 — Recu.me site request](https://github.com/yt-dlp/yt-dlp/issues/10083)
- [ddgs (DuckDuckGo Python search)](https://github.com/deedy5/ddgs)
