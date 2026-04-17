# Coomer Alternatives — Deep Research (April 2026)

_Investigation sparked by the global BGP null-route of Coomer's CDN subnet
`91.149.227.0/24` (Marton Media, Poland) on ~2026-04-17. `coomer.st` itself
still resolves and serves the SPA, but all video downloads redirect to
`nN.coomer.st` which are unreachable from every public vantage point
(confirmed via check-host.net: UAE, Brazil, Bulgaria, Finland, Hong Kong,
Poland, Russia × 2 all report "No route to host" / "Connection timed out")._

## Executive findings

- **No drop-in public-API replacement exists** for Coomer's `/api/v1/{service}/user/{u}/posts`
  OnlyFans/Fansly coverage as of April 2026. The ecosystem gap is structural.
- **Kemono.cr is alive** and has the same API shape, but it was always the
  Patreon/Fanbox side (no OF/Fansly). Keep as a parallel target.
- **Two HTML-scraping alternatives actually work from common networks:**
  1. **Leakedzone.com** — large OF/IG/Snap archive, serves HLS m3u8 directly
     from the main domain (no null-routed shard CDN). Best OF alternative
     we tested.
  2. **Fapello.com** — OF/Snap/IG archive with deterministic numbered posts.
     Image-heavy (good for photo archives; few video-only creators).

- **All Coomer mirror aliases `.st`, `.su`, `.party`** are dead or 307-redirect
  to the dead `.st`. Don't waste cycles probing them.
- **Bunkr, saint2, turbovid** are downstream file-hosts — not creator indexes.
  Port `gallery-dl` extractors if needed as a resolver layer for embedded links.

## Ranked candidate table

| # | Site | Reachable from bare ISP? | Scope | API style | Auth | Architecture | Picked? |
|---|------|---|---|---|---|---|---|
| 1 | **leakedzone.com** | ✅ | OF/IG/Snap | HTML + obfuscated m3u8 URLs on listing page | None | HLS on main domain + BunnyCDN segments | **YES** |
| 2 | **fapello.com** | ✅ | OF/IG/Snap | HTML + numbered posts | None | Own CDN (`fapello.com/content/...`) | **YES** |
| 3 | kemono.cr | ✅ (with DDoS-Guard cookies) | Patreon/Fanbox/SubStar/Fantia | JSON `/api/v1/...` | None (anon) | Own CDN `n*.kemono.cr` | retained (already in codebase) |
| 4 | coomer.st / .su / .party | ❌ (site SPA loads, videos 302→dead CDN) | OF/Fansly/CandFans | was JSON `/api/v1/...` | — | **DEAD — 91.149.227.0/24 null-routed** | retained for auto-recovery |
| 5 | fapello.su / fapello.cc / fapello.io | ❌ (down / collapsed) | — | — | — | — | no |
| 6 | nekohouse.su | ✅ | Fantia circle art | Partial Kemono-fork | Session key for imports only | Own | later |
| 7 | leakedmodels.com | ✅ | OF/celeb | HTML, behind Cloudflare | None (after CF) | Self-host | later |
| 8 | thotsbay.tv | ✅ | OF/Fansly/IG (2.3M videos, 9.2M photos) | HTML per-creator | None | Self-host | later |
| 9 | dirtyship.com | ✅ | OF/celeb | HTML forum/grid hybrid | None | Cloudflare | later |
| 10 | nudostar.com | ✅ | OF photo-heavy | HTML | None | Cloudflare | later |
| 11 | erome.com | ✅ (301) | User-uploaded albums (not per-creator aggregator) | Album-based URLs | Optional login | Self-host | different model |
| 12 | simpcity.su | ✅ (forum-only) | Cross-site forum, not per-creator | — | — | — | out of scope |
| 13 | 4leak.com | Auth-required | — | — | — | — | no |
| 14 | download.leakedzone.com subdomain | ✅ but Cloudflare IUAM challenge | Direct file downloads paired with listing pages | — | — | Cloudflare | fallback for Leakedzone |

## Ship-list of integrated scrapers (April 2026 release)

### Leakedzone — `custom_scrapers.Leakedzone`

**Status: live, tested end-to-end, 48/48 blondie_254 videos downloaded successfully (2026-04-17).**

- `AUTHORITATIVE_USER = True` — username-gated URL path, slug filter skipped
- Probe: `GET /{slug}/video` — counts card anchors
- Enumerate: paginates `?page=N`, extracts `data-video` attribute from each
  card, decodes the obfuscated src → m3u8 URL, populates `stream_url`
  on the ref in a **single pass** (URLs are time-signed and expire)
- Extract: no-op unless ref was serialized without `stream_url`, in which
  case re-fetch the per-video page
- Download: ffmpeg HLS, main domain + BunnyCDN segments, both reachable

**The obfuscation scheme (cracked):**
```
enc = base64(<16 bytes of junk> + "https://.../XXX.m3u8?time=...&sig=...")
      then reverse the whole string
```
Decoder:
```python
def _decode_obfuscated_url(encoded: str) -> str:
    reversed_enc = encoded[::-1]
    for extra in range(4):
        raw = base64.b64decode(reversed_enc + "=" * extra, validate=False)
        for marker in (b"https://", b"http://"):
            idx = raw.find(marker)
            if idx != -1:
                tail = raw[idx:]
                end = len(tail)
                for i, c in enumerate(tail):
                    if c < 0x20 or c in (0x22, 0x27, 0x3c, 0x3e, 0x20):
                        end = i; break
                return tail[:end].decode("utf-8", errors="replace")
    return ""
```

The obfuscated source value is wrapped in HTML-entity-encoded JSON:
```html
<div data-video='{"source":[{"src":"<enc>","type":"application/x-mpegURL"}]}'>
```

**Risks / mitigations:**
- Signed URLs expire ~5 min → we enumerate + download in the same run
  (no stale-URL persistence)
- `download.leakedzone.com` subdomain has a Cloudflare IUAM challenge —
  use the m3u8 path instead
- Cloudflare may tighten later → fallback to `curl_cffi` chrome131
  impersonation if we ever see 403

### Fapello — `custom_scrapers.Fapello`

- `AUTHORITATIVE_USER = True`
- Slug variants: tries `username`, `username.replace("_", "")`,
  `username.replace("_", "-")`, all case-folded
- Probe: `GET /{slug}/` — extracts numbered post links `/{slug}/N/`
- Enumerate: paginates `/page/N/`, grabs per-post URLs
- Extract: fetches post HTML, looks for `src`/`data-src` with `.mp4`
  extension; marks `stream_kind="private"` for image-only posts so the
  retry logic doesn't hammer

**Observed behavior:** Many creators on Fapello are image-only. The
scraper correctly yields 0 video downloads for image-only users like
`blondie254` (who has photo posts, not video posts). This is correct,
not a scraper bug.

### Kemono / Coomer (retained with CDN health check)

Already in the codebase. Added a pre-flight shard-reachability check
via `CoomerKemonoBase._cdn_reachable()` — a 5-second TCP probe to the
`n1.<apex>` shard. If unreachable, `extract_stream()` returns False
with `stream_kind="cdn_blocked"`, and the downloader marks the video
`skip` (not `fail`) so it retries on the next run. Coomer entries will
auto-recover when their BGP is restored, without user intervention.

## What not to integrate (confirmed dead or non-viable in 2026-04)

- **coomer.su** — DNS timeout from every path we tested
- **coomer.party** — 307 redirects to dead `coomer.st`
- **kemono.su** — DNS timeout everywhere
- **fapello.su / .cc / .io** — returning 000 / traffic collapsed -97.68% (SEMrush)
- **thotsbay.com / thotsbay.to** — intermittent (`thotsbay.tv` works, others flap)
- **pornleakedstube.com, fappin.pro, onlyporn.pics** — DNS fail / dead

## Upgrade path

When Coomer's BGP is restored, the existing Coomer scraper + CDN health
check will automatically start using it again (no user action needed —
the pre-flight check will flip from "blocked" to "healthy" and downloads
will proceed). No code change required.

If Fapello / Leakedzone rotate domains (fapello historically moves every
3-6 months), swap `BASE_URL` in the class definition. We do NOT hardcode
the domain in the registry — it's a class attribute, so a one-line
change revives the scraper.

## Verification

The end-to-end scraping + download pipeline was validated live on
2026-04-17:

```
$ python universal_downloader.py blondie_254 --sites leakedzone

HIT: leakedzone (48 videos) @ https://leakedzone.com/blondie_254/video
'blondie_254' done: 48 OK, 0 failed, 0 skipped
```

48 HLS streams pulled via ffmpeg, remuxed to MP4, totalling ~1.2 GB.
No authentication, no VPN, no proxy required — on a bare Kenyan ISP.

## Research sources

- gallery-dl issue #9401 — "coomer.st not working" (still open)
- gallery-dl issues #7902, #7907, #7909 — Coomer / Kemono domain changes
- check-host.net — 8/8 vantage-point reachability test showed global null-route
- SEMrush / Similarweb — Fapello traffic decline per-TLD
- updownradar.com — domain status checks for Coomer, Fapello, Thotsbay
- Live HTTP probes from test IP on 2026-04-17 — reproduced in
  `research/probes_2026_04_17.sh` (this commit)
