# Cam Archive Sites — Deep Research (for custom scrapers)

_Research conducted 2026-04-16. Four research agents covered different site clusters in parallel._

## Site Status Summary

| Site | Status | Strategy |
|---|---|---|
| **camwhores.tv** | LIVE, KVS | KVS decoder |
| **camwhores.co** | LIVE, KVS | KVS decoder |
| **camwhoreshd.com** | LIVE, KVS | KVS decoder |
| **camwhoresbay.com** | LIVE, KVS | KVS decoder (often unobfuscated) |
| **camvideos.tv** | LIVE, KVS | KVS decoder |
| **camhub.cc** | LIVE, KVS | KVS decoder |
| **camwh.com** | LIVE, KVS | KVS decoder |
| **cambro.tv** | LIVE, KVS + Cloudflare | KVS + cloudscraper |
| **camcaps.tv** | LIVE, vtube.to embed | Requires browser (click-to-play) |
| **camcaps.io** | LIVE, vidello.net | HLS URL in embed source |
| **archivebate.com/.cc/.store** | LIVE, Livewire + MixDrop | AJAX → MixDrop decoder |
| **recordbate.com** | LIVE, direct signed MP4 | Parse HTML `<source>` |
| **recu.me** | LIVE, Premium-gated | Cookie + cf_clearance required |
| **recurbate.pro** | LIVE, Premium-gated | Cookie required |
| **sextb.net** | LIVE JAV, JWPlayer iframe | Parse iframe → turboplayers |
| **camvault.to** | LIVE, Premium-gated | Cookie required |
| **sexyc.\***, **cam4capture.com**, **ttbcam.\***, **thetubetv.com**, **bigcam.\***, **cambaby.\***, **videocamcaptures.com** | DEAD | Skip |

---

## 1. KVS (Kernel Video Sharing) Decoder — Covers 9+ Sites

This one decoder works for ALL KVS-based sites. Video URLs are obfuscated with `function/0/` prefix + `license_code`:

```python
import urllib.parse

HASH_LENGTH = 32

def kvs_get_license_token(license_code: str) -> list[int]:
    license_code = license_code.removeprefix('$')
    license_values = [int(c) for c in license_code]
    modlicense = license_code.replace('0', '1')
    middle = len(modlicense) // 2
    fronthalf = int(modlicense[:middle + 1])
    backhalf = int(modlicense[middle:])
    modlicense = str(4 * abs(fronthalf - backhalf))[:middle + 1]
    return [
        (license_values[i + o] + c) % 10
        for i, c in enumerate(int(ch) for ch in modlicense)
        for o in range(4)
    ]

def kvs_get_real_url(video_url: str, license_code: str) -> str:
    if not video_url.startswith('function/0/'):
        return video_url
    parsed = urllib.parse.urlparse(video_url[len('function/0/'):])
    token = kvs_get_license_token(license_code)
    parts = parsed.path.split('/')
    hash_ = parts[3][:HASH_LENGTH]
    indices = list(range(HASH_LENGTH))
    accum = 0
    for src in reversed(range(HASH_LENGTH)):
        accum += token[src]
        dest = (src + accum) % HASH_LENGTH
        indices[src], indices[dest] = indices[dest], indices[src]
    parts[3] = ''.join(hash_[i] for i in indices) + parts[3][HASH_LENGTH:]
    return urllib.parse.urlunparse(parsed._replace(path='/'.join(parts)))
```

**KVS detection fingerprint:** `kt_player.js`, `<meta name="generator" content="KVS CMS"/>`, or inline `var flashvars = {...}`.

**KVS sites share URL patterns:**
- Video: `/videos/{id}/{slug}/` (trailing slash matters)
- Search: `/search/{query}/` or `/search/videos/{query}` (varies)
- Profile: `/models/{username}/`, `/users/{username}/`
- Pagination: `?page=N`, `/latest-updates/{N}/`

**Headers for MP4 download:** UA + `Referer: <video page URL>`. Files support HTTP Range → aria2c `-x16 -s16` works great.

---

## 2. MixDrop Decoder (Cyberdrop-DL style — no jsunpack)

```python
from datetime import datetime, timedelta

def mixdrop_download_url(html: str) -> str:
    """Find the MDCore script block and build the signed download URL."""
    import re
    def between(txt, a, b):
        i = txt.find(a); j = txt.find(b, i + len(a))
        return txt[i+len(a):j] if i >= 0 and j > i else ""
    # Find script containing MDCore.ref
    m = re.search(r"<script[^>]*>([^<]*MDCore\.ref[^<]*)</script>", html, re.DOTALL)
    if not m:
        return ""
    js = m.group(1)
    file_id = between(js, "|v2||", "|")
    parts = between(js, "MDCore||", "|thumbs").split("|")
    secure_key = between(js, f"{file_id}|", "|")
    ts = int((datetime.now() + timedelta(hours=1)).timestamp())
    host = ".".join(parts[:-3])
    ext = parts[-3]
    expires = parts[-1]
    return f"https://s-{host}/v2/{file_id}.{ext}?s={secure_key}&e={expires}&t={ts}"
```

---

## 3. Recordbate.com — Trivial (direct MP4)

URL patterns:
- Performer: `/performer/{username}` (e.g. `/performer/sashabulls`)
- Video: `/videos/{username}{unix_timestamp}` (concatenated)

Extraction: parse `<source src="...b-cdn.net/...?md5=X&expires=Y">` directly from HTML. Token expires ~20 min.

Video card regex: `href="(https://recordbate\.com/videos/([a-z0-9_]+\d+))"`

---

## 4. Archivebate (Livewire + MixDrop)

URL patterns:
- Profile: `/profile/{username}` (literal username from streaming site)
- Video: `/watch/{numeric_id}`

**Livewire pattern** — the page renders empty cards until JS calls `loadVideos`. To emulate:

```python
# 1. GET /profile/{user}, extract CSRF token + Livewire initial-data
csrf = re.search(r'csrf-token.*content="([^"]+)"', html).group(1)
initial = re.findall(r'wire:initial-data="([^"]+)"', html)

# 2. POST to Livewire message endpoint
payload = {
    'fingerprint': component['fingerprint'],
    'serverMemo': component['serverMemo'],
    'updates': [{'type': 'callMethod', 'payload': {'id': 'p1', 'method': 'loadVideos', 'params': []}}],
}
session.post(f'/livewire/message/{component_name}', json=payload,
    headers={'X-CSRF-TOKEN': csrf, 'X-Livewire': 'true'})
```

Every `/watch/{id}` page has a single MixDrop iframe. Use the decoder above.

---

## 5. Camcaps.io — vidello.net HLS

URL patterns:
- Video: `/video/{numeric_id}/{slug}`
- Pagination: `/videos?o=mr&page=N`

Two-hop extraction:
1. Video page → `<iframe src="https://camcaps.io/embed/{hash}">` → another iframe
2. That iframe points to `https://vidello.net/embed-{id}.html`
3. Fetch vidello page → extract `sources: [{file: "https://*.tnmr.org/hls2/.../master.m3u8?t=X&s=Y&e=Z"}]`

Download with ffmpeg + `Referer: https://vidello.net/`.

---

## 6. Recu.me — Premium-gated HLS

Requires manual Cookie setup from paid premium account. Too gated for general automation.

Extraction flow (when authenticated):
1. GET `/video/{id}/play` → extract `data-token` from `<button id="play_button">`
2. GET `/api/video/{id}?token={token}` → returns `<source src="X.m3u8">`
3. Download m3u8 with signed segments — each `.ts` URL needs `&check=` appended:
   ```python
   def sign_ts(url):
       uid = re.search(r'uid=([^&]*)', url).group(1)
       exp = re.search(r'expires=([^&]*)', url).group(1)
       req = re.search(r'request_id=([^&]*)', url).group(1)
       check = req[:4] + uid[2:6] + exp[-4:]
       return url + f"&check={check}"
   ```

Cloudflare Turnstile blocks cloudscraper — need FlareSolverr or Playwright.

---

## 7. Sextb.net (JAV, JWPlayer iframe)

URL patterns:
- Actress: `/actress/{name-with-hyphens}`
- Video: `/{JAV-ID}-rm` (e.g. `/apns-199-rm`)

Extraction:
1. Parse video page → finds iframe to `turboplayers.xyz/t/{hash}`
2. Fetch that → extract `var urlPlay = 'https://e03.etvp.cc/uploads/{hash}.mp4';`
3. Download MP4 with `Referer: https://turboplayers.xyz/`

---

## Sources

- yt-dlp `generic.py` _extract_kvs
- Cyberdrop-DL `_kvs.py`, `mixdrop.py`, `archivebate.py`, `camwhores_dot_tv.py`
- baconator696/Recu-Download (Go → Python port reference)
- Temp530/Archivebate-Scraper (C# Selenium reference)
- Research agents returned ~240KB of detailed findings from parallel WebFetch + WebSearch sweeps
