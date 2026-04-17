#!/usr/bin/env python3
"""
Custom scrapers for cam archive sites NOT supported by yt-dlp.

Each scraper implements:
  - probe(username) -> ProbeHit or None
  - enumerate(profile_url, username, limit) -> List[VideoRef]
  - extract_stream(video_url) -> (stream_url, kind, headers)   # kind = "mp4" | "hls"
  - (downloads use shared aria2c for mp4, ffmpeg for hls)

Supported clusters:
  1. KVS family (camwhores.tv, .co, HD, camwhoresbay, camvideos, camhub, camwh, cambro, etc.)
  2. Recordbate (trivial direct MP4)
  3. Archivebate (Livewire + MixDrop)
  4. Camcaps.io (vidello.net HLS)
  5. Extensible base class for more
"""
from __future__ import annotations

import html as _html
import json
import logging
import os
import re
import time
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

import requests
import cloudscraper

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


# ── Data types (duplicated from universal_downloader for standalone use) ───
@dataclass
class VideoRef:
    site: str
    video_id: str
    video_url: str
    title: str = ""
    uploader: str = ""
    uploader_id: str = ""
    duration: float = 0.0
    performer: str = ""
    # Stream-specific fields populated after extract_stream
    stream_url: str = ""
    stream_kind: str = ""   # "mp4" | "hls" | ""
    stream_headers: Dict[str, str] = field(default_factory=dict)

    @property
    def global_id(self) -> str:
        return f"{self.site}|{self.video_id}"


@dataclass
class ProbeHit:
    site: str
    url: str
    entry_count: int = 0
    uploader_id: str = ""


# ── Shared helpers ────────────────────────────────────────────────────────

def kvs_get_license_token(license_code: str) -> List[int]:
    """KVS license_code → int[] of swap offsets.
    Ported from yt-dlp's _extract_kvs() in extractor/generic.py."""
    license_code = license_code.lstrip("$")
    license_values = [int(c) for c in license_code]
    modlicense = license_code.replace("0", "1")
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
    """Unshuffle the obfuscated hash in a KVS 'function/0/...' URL."""
    if not video_url.startswith("function/0/"):
        return video_url
    parsed = urllib.parse.urlparse(video_url[len("function/0/"):])
    token = kvs_get_license_token(license_code)
    parts = parsed.path.split("/")
    if len(parts) < 4:
        return video_url
    HASH_LEN = 32
    hash_ = parts[3][:HASH_LEN]
    if len(hash_) < HASH_LEN:
        return video_url
    indices = list(range(HASH_LEN))
    accum = 0
    for src in reversed(range(HASH_LEN)):
        accum += token[src]
        dest = (src + accum) % HASH_LEN
        indices[src], indices[dest] = indices[dest], indices[src]
    parts[3] = "".join(hash_[i] for i in indices) + parts[3][HASH_LEN:]
    return urllib.parse.urlunparse(parsed._replace(path="/".join(parts)))


def parse_kvs_flashvars(html: str) -> Dict[str, str]:
    """Extract the flashvars object from a KVS page. Returns dict of str fields."""
    # Match both `var flashvars = { ... };` and `flashvars = {...`
    m = re.search(r"(?:var\s+)?flashvars\s*=\s*(\{[^}]+\})", html, re.DOTALL)
    if not m:
        return {}
    block = m.group(1)
    out: Dict[str, str] = {}
    # key: 'value', — handle single and double quotes
    for km in re.finditer(r"(\w+)\s*:\s*(['\"])([^'\"]*)\2", block):
        out[km.group(1)] = km.group(3)
    return out


def mixdrop_build_url(html: str) -> str:
    """Extract the direct MP4 URL from a mixdrop embed page.
    Uses the Cyberdrop-DL MDCore.ref algorithm (no jsunpack needed)."""
    # Find the <script> block that contains MDCore.ref assembly
    m = re.search(r"<script[^>]*>([^<]*MDCore\.ref[^<]*)</script>", html, re.DOTALL)
    if not m:
        return ""
    js = m.group(1)

    def between(txt: str, a: str, b: str) -> str:
        i = txt.find(a)
        if i < 0:
            return ""
        i += len(a)
        j = txt.find(b, i)
        return txt[i:j] if j >= 0 else ""

    file_id = between(js, "|v2||", "|")
    if not file_id:
        return ""
    parts = between(js, "MDCore||", "|thumbs").split("|")
    if len(parts) < 4:
        return ""
    secure_key = between(js, f"{file_id}|", "|")
    ts = int((datetime.now() + timedelta(hours=1)).timestamp())
    host = ".".join(parts[:-3])
    ext = parts[-3]
    expires = parts[-1]
    return f"https://s-{host}/v2/{file_id}.{ext}?s={secure_key}&e={expires}&t={ts}"


# ── Base class ────────────────────────────────────────────────────────────

def load_netscape_cookies(cookies_file: str) -> Optional[MozillaCookieJar]:
    """Load a Netscape-format cookies.txt file. Used to inject authenticated
    sessions from the user's browser (export via 'Get cookies.txt LOCALLY'
    Chrome extension or Firefox 'cookies.txt' addon)."""
    if not cookies_file or not os.path.exists(cookies_file):
        return None
    try:
        # Normalize: Netscape format requires the first line to be a magic header
        # Many browser extensions write the file without it — add it if missing.
        content = open(cookies_file, encoding="utf-8").read()
        if not content.startswith("# Netscape HTTP Cookie File") and not content.startswith("# HTTP Cookie File"):
            # Write a temporary copy with the header
            tmp_path = cookies_file + ".normalized"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write("# Netscape HTTP Cookie File\n")
                f.write(content)
            load_path = tmp_path
        else:
            load_path = cookies_file
        cj = MozillaCookieJar(load_path)
        cj.load(ignore_discard=True, ignore_expires=True)
        return cj
    except Exception:
        return None


class SiteScraper(ABC):
    """Abstract base. Subclasses must define NAME, BASE_URL, CATEGORY."""
    NAME: str = ""
    BASE_URL: str = ""
    CATEGORY: str = "adult"
    USE_CLOUDSCRAPER: bool = False
    # Path patterns with {u} substitution for username
    PROFILE_PATTERNS: List[str] = []
    # Minimum playlist size to treat a profile as real (not a placeholder)
    MIN_ENTRIES: int = 1
    # Name of the cookie domain this scraper cares about (substring match against
    # cookie domains). If empty, all cookies loaded are used.
    COOKIE_DOMAIN: str = ""

    def __init__(self, log: logging.Logger, cookies_file: str = ""):
        self.log = log
        self._session: Optional[requests.Session] = None
        self.cookies_file = cookies_file
        self._cookie_jar: Optional[MozillaCookieJar] = None
        if cookies_file:
            self._cookie_jar = load_netscape_cookies(cookies_file)
            if self._cookie_jar:
                # Count cookies matching our domain
                if self.COOKIE_DOMAIN:
                    n = sum(1 for c in self._cookie_jar if self.COOKIE_DOMAIN in c.domain)
                    if n:
                        log.debug(f"  [{self.NAME}] loaded {n} cookies for domain {self.COOKIE_DOMAIN}")

    def _make_session(self) -> requests.Session:
        if self.USE_CLOUDSCRAPER:
            s = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows"})
        else:
            s = requests.Session()
        s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        # Inject cookies from cookies.txt if provided
        if self._cookie_jar:
            for c in self._cookie_jar:
                if self.COOKIE_DOMAIN and self.COOKIE_DOMAIN not in c.domain:
                    continue
                try:
                    s.cookies.set(c.name, c.value, domain=c.domain, path=c.path or "/")
                except Exception:
                    pass
        return s

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = self._make_session()
        return self._session

    # -- contract -------------------------------------------------------

    @abstractmethod
    def probe(self, username: str) -> Optional[ProbeHit]:
        """Try to find a profile for `username`. Return ProbeHit or None."""

    @abstractmethod
    def enumerate(self, hit: ProbeHit, username: str, limit: int) -> List[VideoRef]:
        """List videos for a profile."""

    @abstractmethod
    def extract_stream(self, video: VideoRef) -> bool:
        """Populate video.stream_url / .stream_kind / .stream_headers. Returns success."""


# ── KVS-family scraper ────────────────────────────────────────────────────
class KVSScraper(SiteScraper):
    """Shared scraper for all KVS (Kernel Video Sharing) based sites."""
    CATEGORY = "adult"
    # 1 entry is valid if we got here via the exact URL with no redirect to
    # /notfound/. We already filter those out via _is_valid_profile_response.
    MIN_ENTRIES = 1

    # Subclasses override these
    PROFILE_PATTERNS = [
        "{base}/models/{u}/",
        "{base}/members/{u}/",
        "{base}/users/{u}/",
    ]
    # URL patterns for video pages (used to parse listing HTML)
    VIDEO_LINK_RE = re.compile(r'href="([^"]*/videos/(\d+)/[^"]*/?)"')
    # Pagination pattern for listing pages
    PAGE_PARAM = "?page={n}"

    def _listing_url(self, base_profile_url: str, page: int) -> str:
        if page <= 1:
            return base_profile_url
        sep = "&" if "?" in base_profile_url else "?"
        return f"{base_profile_url.rstrip('/')}/{sep}page={page}"

    # Sentinel path fragments that indicate the server redirected to a "not
    # found" / "missing user" fallback page. If the final URL contains any of
    # these, the probe is a false positive.
    NOT_FOUND_MARKERS: List[str] = [
        "/notfound/", "/not-found/", "/404/", "/error/",
        "user_missing", "invalid_search", "no_results",
    ]

    def _is_valid_profile_response(self, url_requested: str, r: requests.Response) -> bool:
        """Reject responses that redirected to a generic 'not found' fallback page.
        camcaps.io/.tv and similar sites 301 missing users to /notfound/user_missing
        which is a template page showing 8 popular videos — those are NOT the user's."""
        final_url = r.url
        for marker in self.NOT_FOUND_MARKERS:
            if marker in final_url:
                return False
        # Also reject if the final URL doesn't reasonably match the requested one
        # (account for trailing slash, query params)
        req_path = urllib.parse.urlparse(url_requested).path.rstrip("/").lower()
        final_path = urllib.parse.urlparse(final_url).path.rstrip("/").lower()
        if req_path and final_path and not (
            req_path == final_path
            or req_path in final_path
            or final_path in req_path
        ):
            # Very different paths — probably redirected to an unrelated page
            self.log.debug(f"  [{self.NAME}] redirect mismatch {req_path} -> {final_path}")
            return False
        return True

    def probe(self, username: str) -> Optional[ProbeHit]:
        """Try each profile URL pattern. Return the first that has >= MIN_ENTRIES videos."""
        base = self.BASE_URL.rstrip("/")
        candidates: List[Tuple[str, int]] = []  # (url, count)
        for pat in self.PROFILE_PATTERNS:
            url = pat.format(base=base, u=username)
            try:
                r = self.session.get(url, timeout=20, allow_redirects=True)
            except Exception as e:
                self.log.debug(f"  [{self.NAME}] probe {url}: {type(e).__name__}")
                continue
            if r.status_code != 200 or len(r.text) < 1000:
                continue
            if not self._is_valid_profile_response(url, r):
                self.log.debug(f"  [{self.NAME}] probe {url}: rejected (not-found redirect)")
                continue
            matches = self.VIDEO_LINK_RE.findall(r.text)
            unique_ids = {vid for _, vid in matches}
            count = len(unique_ids)
            if count >= self.MIN_ENTRIES:
                candidates.append((url, count))
                self.log.debug(f"  [{self.NAME}] probe hit {url} -> {count} videos")
        if not candidates:
            return None
        # Pick the URL with the most videos
        candidates.sort(key=lambda x: -x[1])
        url, count = candidates[0]
        return ProbeHit(site=self.NAME, url=url, entry_count=count)

    def enumerate(self, hit: ProbeHit, username: str, limit: int) -> List[VideoRef]:
        videos: List[VideoRef] = []
        seen: set = set()
        max_pages = 100   # increased — user has tag pages with 100+ videos
        for page in range(1, max_pages + 1):
            url = self._listing_url(hit.url, page)
            try:
                r = self.session.get(url, timeout=20)
            except Exception as e:
                self.log.debug(f"  [{self.NAME}] enum page {page}: {e}")
                break
            if r.status_code != 200:
                break
            matches = self.VIDEO_LINK_RE.findall(r.text)
            new = 0
            for path, vid in matches:
                if vid in seen:
                    continue
                seen.add(vid)
                new += 1
                full = path if path.startswith("http") else self.BASE_URL.rstrip("/") + path
                videos.append(VideoRef(
                    site=self.NAME,
                    video_id=vid,
                    video_url=full,
                    performer=username,
                ))
                if limit and len(videos) >= limit:
                    return videos
            if new == 0:
                break
            time.sleep(0.5)
        return videos

    def extract_stream(self, video: VideoRef) -> bool:
        """Fetch the video page, parse flashvars, decode the KVS URL.
        If the page doesn't have flashvars but embeds an iframe to another
        KVS site (e.g. camhub.cc embeds camhub.world), follow the iframe."""
        try:
            r = self.session.get(video.video_url, timeout=20)
        except Exception as e:
            self.log.debug(f"  [{self.NAME}] stream fetch: {e}")
            return False
        if r.status_code != 200:
            self.log.debug(f"  [{self.NAME}] stream HTTP {r.status_code}")
            return False
        html = r.text

        # Title (set early so private-skip message shows it)
        mt = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
        if mt:
            video.title = _html.unescape(mt.group(1))

        # Private-video detection — camwhores.tv family shows a "This video is
        # a private video uploaded by X" message with no flashvars. Private
        # requires friend-authenticated cookies. If cookies are provided but
        # we STILL see the private message, we're either not friends with the
        # uploader or the cookies are stale.
        is_private_msg = re.search(
            r"(private video uploaded by|this video is private|members only|"
            r"only for friends|only friends can watch|friends only)",
            html, re.IGNORECASE,
        )
        if is_private_msg:
            has_auth_cookies = bool(self._cookie_jar) and any(
                self.COOKIE_DOMAIN in c.domain for c in (self._cookie_jar or [])
            )
            if has_auth_cookies:
                # We HAVE cookies but still see private — we're not friends
                # with the uploader. This video is genuinely inaccessible to us.
                self.log.debug(
                    f"  [{self.NAME}] {video.video_id} is PRIVATE "
                    f"(not a friend of uploader even with auth cookies)"
                )
            else:
                self.log.debug(f"  [{self.NAME}] {video.video_id} is PRIVATE (login required)")
            video.stream_kind = "private"
            return False

        fv = parse_kvs_flashvars(html)
        # Two-hop: if no flashvars on main page, try the embed iframe.
        # Filter out ad iframes (traffic.*, banner, promo, live-banner) and
        # prefer /embed/ paths which are typical KVS embed URLs.
        if not fv:
            iframe_candidates = []
            for m in re.finditer(r'<iframe[^>]+src="(https?://[^"]+)"', html, re.IGNORECASE):
                u = m.group(1)
                ul = u.lower()
                # Blacklist ad/tracker iframes
                if any(bad in ul for bad in [
                    "traffic.", "banner", "promo", "live-banner", "aff_id=",
                    "gstatic.", "doubleclick", "adserver", "googlesyn",
                    "cam4pays", "affiliate", "cmsrtb",
                ]):
                    continue
                iframe_candidates.append(u)
            # Prefer URLs containing /embed/
            iframe_candidates.sort(key=lambda u: (0 if "/embed" in u.lower() else 1))
            for iframe_url in iframe_candidates[:3]:
                try:
                    ir = self.session.get(iframe_url, timeout=20,
                                          headers={"Referer": video.video_url})
                    if ir.status_code != 200:
                        continue
                    if re.search(r"(private|members only|friends only)", ir.text, re.IGNORECASE):
                        video.stream_kind = "private"
                        return False
                    fv2 = parse_kvs_flashvars(ir.text)
                    if fv2:
                        fv = fv2
                        video.video_url = iframe_url
                        html = ir.text
                        break
                except Exception as e:
                    self.log.debug(f"  [{self.NAME}] iframe fetch {iframe_url}: {e}")

        if not fv:
            self.log.debug(f"  [{self.NAME}] no flashvars in video page")
            return False

        license_code = fv.get("license_code", "")
        # Collect all video URLs (ordered): prefer video_url, else video_alt_url*
        candidates: List[Tuple[str, str]] = []  # (quality_label, url)
        for key, val in fv.items():
            if not re.match(r"^video_(url|alt_url\d*)$", key):
                continue
            if not val or "/get_file/" not in val:
                continue
            real = kvs_get_real_url(val, license_code) if license_code else val
            # Resolve relative URL
            real = urllib.parse.urljoin(video.video_url, real)
            label = fv.get(f"{key}_text", key)
            candidates.append((label, real))

        if not candidates:
            self.log.debug(f"  [{self.NAME}] no video_url in flashvars")
            return False

        # Pick highest quality — prefer "720p"/"HD"/"480p" over "360p"
        def quality_score(lbl: str) -> int:
            lbl_l = lbl.lower()
            for q, score in [("1080", 100), ("720", 80), ("hd", 70), ("480", 60),
                             ("360", 40), ("240", 20)]:
                if q in lbl_l:
                    return score
            return 0
        candidates.sort(key=lambda c: -quality_score(c[0]))
        video.stream_url = candidates[0][1]
        video.stream_kind = "mp4"
        video.stream_headers = {
            "Referer": video.video_url,
            "User-Agent": USER_AGENT,
        }
        if not video.title:
            video.title = fv.get("video_title", "") or f"{self.NAME}-{video.video_id}"
        self.log.debug(f"  [{self.NAME}] stream OK {video.stream_url[:80]}")
        return True


# Individual KVS site classes (URL variations)

class CamwhoresTV(KVSScraper):
    NAME = "camwhores_tv"
    BASE_URL = "https://www.camwhores.tv"
    COOKIE_DOMAIN = "camwhores.tv"
    # /tags/{username}/ is actually the most reliable on camwhores.tv for cam
    # performers. /models/ and /members/ often 404 while /tags/ finds 10-30+.
    PROFILE_PATTERNS = [
        "{base}/tags/{u}/",
        "{base}/models/{u}/",
        "{base}/members/{u}/",
    ]


class CamwhoresCO(KVSScraper):
    NAME = "camwhores_co"
    BASE_URL = "https://www.camwhores.co"
    PROFILE_PATTERNS = [
        "{base}/tags/{u}/",
        "{base}/models/{u}/",
        "{base}/members/{u}/",
    ]


class CamwhoresHD(KVSScraper):
    NAME = "camwhoreshd"
    BASE_URL = "https://camwhoreshd.com"
    PROFILE_PATTERNS = [
        "{base}/tags/{u}/",
        "{base}/models/{u}/",
        "{base}/members/{u}/",
    ]


class CamwhoresBay(KVSScraper):
    NAME = "camwhoresbay"
    BASE_URL = "https://www.camwhoresbay.com"
    PROFILE_PATTERNS = [
        "{base}/tags/{u}/",
        "{base}/models/{u}/",
        "{base}/members/{u}/",
    ]


class CamVideosTV(KVSScraper):
    NAME = "camvideos_tv"
    BASE_URL = "https://www.camvideos.tv"
    PROFILE_PATTERNS = [
        "{base}/tags/{u}/",
        "{base}/models/{u}/",
        "{base}/members/{u}/",
    ]
    # camvideos uses /{id}/{slug}/ not /videos/{id}/{slug}/
    VIDEO_LINK_RE = re.compile(r'href="(/(\d+)/[a-z0-9-]+/)"')


class CamhubCC(KVSScraper):
    NAME = "camhub_cc"
    BASE_URL = "https://www.camhub.cc"
    PROFILE_PATTERNS = [
        "{base}/tags/{u}/",
        "{base}/models/{u}/",
        "{base}/members/{u}/",
    ]


class CamwhCom(KVSScraper):
    NAME = "camwh_com"
    BASE_URL = "https://camwh.com"
    PROFILE_PATTERNS = [
        "{base}/tags/{u}/",
        "{base}/models/{u}/",
        "{base}/members/{u}/",
    ]


class CambroTV(KVSScraper):
    NAME = "cambro_tv"
    BASE_URL = "https://www.cambro.tv"
    USE_CLOUDSCRAPER = True   # CF challenge
    PROFILE_PATTERNS = [
        "{base}/tags/{u}/",
        "{base}/models/{u}/",
        "{base}/members/{u}/",
    ]
    VIDEO_LINK_RE = re.compile(r'href="(/(\d+)/[a-z0-9-]+/)"')


class CamStreamsTV(KVSScraper):
    """camstreams.tv — another KVS-family tube."""
    NAME = "camstreams_tv"
    BASE_URL = "https://camstreams.tv"
    PROFILE_PATTERNS = [
        "{base}/search/?q={u}",
        "{base}/tags/{u}/",
        "{base}/models/{u}/",
    ]
    VIDEO_LINK_RE = re.compile(r'href="(/videos/(\d+)/[^"]+/)"')


# Additional KVS-family sites discovered in second research pass
class CamwhoresVideo(KVSScraper):
    """camwhores.video — KVS, often has MORE videos per tag than .tv."""
    NAME = "camwhores_video"
    BASE_URL = "https://www.camwhores.video"
    PROFILE_PATTERNS = [
        "{base}/tags/{u}/",
        "{base}/search/{u}/",
        "{base}/models/{u}/",
        "{base}/members/{u}/",
    ]


class CamwhoresBayTV(KVSScraper):
    """camwhoresbay.tv — sister of .com; often works when .com 403s."""
    NAME = "camwhoresbay_tv"
    BASE_URL = "https://www.camwhoresbay.tv"
    PROFILE_PATTERNS = [
        "{base}/tags/{u}/",
        "{base}/search/{u}/",
        "{base}/models/{u}/",
        "{base}/members/{u}/",
    ]


class CamwhoresBZ(KVSScraper):
    """camwhores.bz — another KVS mirror of the family."""
    NAME = "camwhores_bz"
    BASE_URL = "https://camwhores.bz"
    PROFILE_PATTERNS = [
        "{base}/tags/{u}/",
        "{base}/search/{u}/",
        "{base}/models/{u}/",
    ]


class CamwhoresCloud(KVSScraper):
    """camwhorescloud.com — KVS, aggregates CB/MFC/BC/SC/C4/LJ/etc.
    Search endpoint is significantly broader than tag endpoint on this site."""
    NAME = "camwhorescloud"
    BASE_URL = "https://www.camwhorescloud.com"
    PROFILE_PATTERNS = [
        "{base}/videos/search/{u}/",
        "{base}/search/{u}/",
        "{base}/tags/{u}/",
        "{base}/models/{u}/",
    ]


class Porntrex(KVSScraper):
    """porntrex.com — KVS, large general adult archive."""
    NAME = "porntrex"
    BASE_URL = "https://www.porntrex.com"
    PROFILE_PATTERNS = [
        "{base}/models/{u}/",
        "{base}/tags/{u}/",
        "{base}/search/{u}/",
    ]


class CamCapsTV(KVSScraper):
    """CamCaps.tv uses KVS-style listings but video player goes through vtube.to.
    We grab the embed URL from the video page and leave vtube extraction to the
    VideoEmbedResolver (or skip if we can't break vtube — they use click-to-play)."""
    NAME = "camcaps_tv"
    BASE_URL = "https://camcaps.tv"
    PROFILE_PATTERNS = [
        "{base}/user/{u}/videos",
        "{base}/user/{u}",
    ]
    VIDEO_LINK_RE = re.compile(r'href="(/video/(\d+)/[a-z0-9-]+)"')

    def extract_stream(self, video: VideoRef) -> bool:
        """Video page has an iframe to vtube.to/embed-XXX.html.
        vtube has click-to-play JS so direct scraping fails without a browser.
        We try to find fallback sources in the page first; return False otherwise."""
        try:
            r = self.session.get(video.video_url, timeout=20)
        except Exception as e:
            self.log.debug(f"  [{self.NAME}] fetch: {e}")
            return False
        if r.status_code != 200:
            return False
        html = r.text

        # Title
        mt = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
        if mt:
            video.title = _html.unescape(mt.group(1))

        # Check for KVS flashvars first (some sites embed KVS directly)
        fv = parse_kvs_flashvars(html)
        if fv and fv.get("video_url"):
            lic = fv.get("license_code", "")
            real = kvs_get_real_url(fv["video_url"], lic) if lic else fv["video_url"]
            video.stream_url = urllib.parse.urljoin(video.video_url, real)
            video.stream_kind = "mp4"
            video.stream_headers = {"Referer": video.video_url, "User-Agent": USER_AGENT}
            return True

        # Look for vtube iframe — we can't currently extract without a browser
        m = re.search(r'<iframe[^>]+src="(https?://vtube\.[^"]+)"', html)
        if m:
            self.log.debug(f"  [{self.NAME}] vtube embed {m.group(1)} — needs browser")
            return False

        return False


# ── Recordbate.com — direct MP4 ──────────────────────────────────────────

class Recordbate(SiteScraper):
    NAME = "recordbate"
    BASE_URL = "https://recordbate.com"
    CATEGORY = "adult"
    USE_CLOUDSCRAPER = True
    MIN_ENTRIES = 1
    PROFILE_PATTERNS = [
        "{base}/performer/{u}",
    ]
    VIDEO_LINK_RE = re.compile(r'href="(https://recordbate\.com/videos/([a-z0-9_]+\d+))"')

    def probe(self, username: str) -> Optional[ProbeHit]:
        url = f"{self.BASE_URL}/performer/{username}"
        try:
            r = self.session.get(url, timeout=20)
        except Exception as e:
            self.log.debug(f"  [{self.NAME}] probe: {e}")
            return None
        if r.status_code != 200:
            return None
        matches = self.VIDEO_LINK_RE.findall(r.text)
        if not matches:
            return None
        unique = {vid for _, vid in matches}
        return ProbeHit(site=self.NAME, url=url, entry_count=len(unique))

    def enumerate(self, hit: ProbeHit, username: str, limit: int) -> List[VideoRef]:
        videos: List[VideoRef] = []
        seen: set = set()
        for page in range(1, 20):
            url = hit.url if page == 1 else f"{hit.url}?page={page}"
            try:
                r = self.session.get(url, timeout=20)
            except Exception:
                break
            if r.status_code != 200:
                break
            matches = self.VIDEO_LINK_RE.findall(r.text)
            new = 0
            for full_url, vid in matches:
                if vid in seen:
                    continue
                seen.add(vid)
                new += 1
                videos.append(VideoRef(
                    site=self.NAME,
                    video_id=vid,
                    video_url=full_url,
                    performer=username,
                ))
                if limit and len(videos) >= limit:
                    return videos
            if new == 0:
                break
            time.sleep(0.3)
        return videos

    def extract_stream(self, video: VideoRef) -> bool:
        try:
            r = self.session.get(video.video_url, timeout=20)
        except Exception as e:
            self.log.debug(f"  [{self.NAME}] stream: {e}")
            return False
        if r.status_code != 200:
            return False
        html = r.text
        # <source src="https://*.b-cdn.net/..." type="video/mp4">
        m = re.search(r'<source\s+src="(https?://[^"]+?\.mp4[^"]*)"\s+type="video/mp4"', html)
        if not m:
            # Fallback: any mp4 URL with expires/md5
            m = re.search(r'"(https?://[^"]+\.b-cdn\.net/[^"]+\.mp4\?md5=[^"]+)"', html)
        if not m:
            return False
        video.stream_url = _html.unescape(m.group(1))
        video.stream_kind = "mp4"
        video.stream_headers = {"Referer": video.video_url, "User-Agent": USER_AGENT}
        mt = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
        if mt:
            video.title = _html.unescape(mt.group(1))
        elif not video.title:
            video.title = f"recordbate-{video.video_id}"
        return True


# ── Archivebate (Livewire + MixDrop) ─────────────────────────────────────

class Archivebate(SiteScraper):
    NAME = "archivebate"
    BASE_URL = "https://archivebate.com"
    CATEGORY = "adult"
    USE_CLOUDSCRAPER = True
    MIN_ENTRIES = 1
    PROFILE_PATTERNS = [
        "{base}/profile/{u}",
    ]

    def _get_csrf(self, html: str) -> str:
        m = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
        return m.group(1) if m else ""

    def _extract_livewire_components(self, html: str) -> List[dict]:
        """Find all wire:initial-data="..." blobs and decode them."""
        out = []
        for m in re.finditer(r'wire:initial-data="([^"]+)"', html):
            try:
                raw = _html.unescape(m.group(1))
                out.append(json.loads(raw))
            except Exception:
                pass
        return out

    def probe(self, username: str) -> Optional[ProbeHit]:
        url = f"{self.BASE_URL}/profile/{username}"
        try:
            r = self.session.get(url, timeout=20)
        except Exception as e:
            self.log.debug(f"  [{self.NAME}] probe: {e}")
            return None
        if r.status_code != 200:
            return None
        # Look for video links directly in HTML (server-rendered) — may find some
        direct_matches = re.findall(r'href="(https?://archivebate\.com/watch/(\d+))"', r.text)
        direct_matches += re.findall(r'href="(/watch/(\d+))"', r.text)
        if direct_matches:
            unique = {vid for _, vid in direct_matches}
            if len(unique) >= self.MIN_ENTRIES:
                return ProbeHit(site=self.NAME, url=url, entry_count=len(unique))
        # If empty, try Livewire call
        components = self._extract_livewire_components(r.text)
        if not components:
            return None
        # Profile page should have a component with profile.model-videos or similar
        csrf = self._get_csrf(r.text)
        if not csrf:
            return None
        for comp in components:
            fp = comp.get("fingerprint", {})
            name = fp.get("name", "")
            if "profile" in name.lower() or "video" in name.lower():
                try:
                    resp = self._livewire_call(comp, csrf, url, "loadVideos", [])
                except Exception as e:
                    self.log.debug(f"  [{self.NAME}] livewire call: {e}")
                    continue
                if not resp:
                    continue
                html_frag = (resp.get("effects") or {}).get("html", "")
                count = len(re.findall(r'href="[^"]*/watch/(\d+)"', html_frag))
                if count >= self.MIN_ENTRIES:
                    return ProbeHit(site=self.NAME, url=url, entry_count=count)
        return None

    def _livewire_call(self, component: dict, csrf: str, referer: str,
                        method: str, params: list) -> Optional[dict]:
        fp = component["fingerprint"]
        name = fp["name"]
        payload = {
            "fingerprint": fp,
            "serverMemo": component["serverMemo"],
            "updates": [{
                "type": "callMethod",
                "payload": {"id": "p1", "method": method, "params": params},
            }],
        }
        headers = {
            "Content-Type": "application/json",
            "X-CSRF-TOKEN": csrf,
            "X-Livewire": "true",
            "Accept": "application/json",
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
        }
        r = self.session.post(
            f"{self.BASE_URL}/livewire/message/{name}",
            json=payload, headers=headers, timeout=20,
        )
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except Exception:
            return None

    def enumerate(self, hit: ProbeHit, username: str, limit: int) -> List[VideoRef]:
        videos: List[VideoRef] = []
        seen: set = set()
        try:
            r = self.session.get(hit.url, timeout=20)
        except Exception:
            return []
        if r.status_code != 200:
            return []

        # Try direct HTML parse first
        for full, vid in re.findall(r'href="(https?://archivebate\.com/watch/(\d+))"', r.text):
            if vid in seen:
                continue
            seen.add(vid)
            videos.append(VideoRef(
                site=self.NAME, video_id=vid, video_url=full, performer=username,
            ))
            if limit and len(videos) >= limit:
                return videos

        if videos:
            return videos

        # Fallback: Livewire loadVideos
        csrf = self._get_csrf(r.text)
        components = self._extract_livewire_components(r.text)
        for comp in components:
            fp = comp.get("fingerprint", {})
            if "profile" not in fp.get("name", "").lower():
                continue
            resp = self._livewire_call(comp, csrf, hit.url, "loadVideos", [])
            if not resp:
                continue
            frag = (resp.get("effects") or {}).get("html", "")
            for vid_match in re.finditer(r'href="(?:https?://archivebate\.com)?(/watch/(\d+))"', frag):
                vid = vid_match.group(2)
                if vid in seen:
                    continue
                seen.add(vid)
                full_url = f"{self.BASE_URL}/watch/{vid}"
                videos.append(VideoRef(
                    site=self.NAME, video_id=vid, video_url=full_url, performer=username,
                ))
                if limit and len(videos) >= limit:
                    return videos
        return videos

    def extract_stream(self, video: VideoRef) -> bool:
        try:
            r = self.session.get(video.video_url, timeout=20)
        except Exception as e:
            self.log.debug(f"  [{self.NAME}] stream fetch: {e}")
            return False
        if r.status_code != 200:
            return False
        html = r.text

        mt = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
        if mt:
            video.title = _html.unescape(mt.group(1))

        # Find the MixDrop iframe
        mm = re.search(r'<iframe[^>]+src="(https?://mixdrop\.[a-z.]+/e/[a-z0-9]+)"', html)
        if not mm:
            # Might use /f/ form
            mm = re.search(r'<iframe[^>]+src="(https?://mixdrop\.[a-z.]+/[ef]/[a-z0-9]+)"', html)
        if not mm:
            self.log.debug(f"  [{self.NAME}] no mixdrop iframe")
            return False

        mixdrop_url = mm.group(1)
        # Convert /f/ to /e/
        mixdrop_url = mixdrop_url.replace("/f/", "/e/")

        # Fetch mixdrop embed page
        try:
            mr = self.session.get(mixdrop_url, timeout=20, verify=False,
                                  headers={"Referer": video.video_url})
        except Exception as e:
            self.log.debug(f"  [{self.NAME}] mixdrop: {e}")
            return False
        if mr.status_code != 200:
            self.log.debug(f"  [{self.NAME}] mixdrop HTTP {mr.status_code}")
            return False

        stream = mixdrop_build_url(mr.text)
        if not stream:
            # Try fallback: directly in mixdrop page as window.source = "..." or similar
            m2 = re.search(r'MDCore\.\w+\s*=\s*"(//[^"]+\.(?:mp4|m3u8)[^"]*)"', mr.text)
            if m2:
                stream = "https:" + m2.group(1)
        if not stream:
            return False

        video.stream_url = stream
        video.stream_kind = "mp4" if ".mp4" in stream.lower() else "hls"
        video.stream_headers = {
            "Referer": mixdrop_url,
            "User-Agent": USER_AGENT,
        }
        return True


# ── CamCaps.io (vidello.net HLS) ─────────────────────────────────────────

class CamCapsIO(SiteScraper):
    NAME = "camcaps_io"
    BASE_URL = "https://camcaps.io"
    CATEGORY = "adult"
    USE_CLOUDSCRAPER = True
    MIN_ENTRIES = 2
    PROFILE_PATTERNS = [
        "{base}/models/{u}/",
        "{base}/user/{u}",
        "{base}/user/{u}/videos",
    ]
    VIDEO_LINK_RE = re.compile(r'href="(/video/(\d+)/[a-z0-9-]+)"')

    # Inherit NOT_FOUND_MARKERS + _is_valid_profile_response behavior
    NOT_FOUND_MARKERS = KVSScraper.NOT_FOUND_MARKERS

    def _is_valid_profile_response(self, url_requested: str, r: requests.Response) -> bool:
        final_url = r.url
        for marker in self.NOT_FOUND_MARKERS:
            if marker in final_url:
                return False
        req_path = urllib.parse.urlparse(url_requested).path.rstrip("/").lower()
        final_path = urllib.parse.urlparse(final_url).path.rstrip("/").lower()
        if req_path and final_path and not (
            req_path == final_path
            or req_path in final_path
            or final_path in req_path
        ):
            return False
        return True

    def probe(self, username: str) -> Optional[ProbeHit]:
        best: Optional[ProbeHit] = None
        base = self.BASE_URL.rstrip("/")
        for pat in self.PROFILE_PATTERNS:
            url = pat.format(base=base, u=username)
            try:
                r = self.session.get(url, timeout=20)
            except Exception:
                continue
            if r.status_code != 200 or len(r.text) < 1000:
                continue
            if not self._is_valid_profile_response(url, r):
                self.log.debug(f"  [{self.NAME}] probe {url}: rejected (not-found redirect)")
                continue
            matches = self.VIDEO_LINK_RE.findall(r.text)
            unique = {vid for _, vid in matches}
            if len(unique) >= self.MIN_ENTRIES:
                if best is None or len(unique) > best.entry_count:
                    best = ProbeHit(site=self.NAME, url=url, entry_count=len(unique))
        return best

    def enumerate(self, hit: ProbeHit, username: str, limit: int) -> List[VideoRef]:
        videos: List[VideoRef] = []
        seen: set = set()
        for page in range(1, 20):
            url = hit.url if page == 1 else f"{hit.url.rstrip('/')}/?page={page}"
            try:
                r = self.session.get(url, timeout=20)
            except Exception:
                break
            if r.status_code != 200:
                break
            matches = self.VIDEO_LINK_RE.findall(r.text)
            new = 0
            for path, vid in matches:
                if vid in seen:
                    continue
                seen.add(vid)
                new += 1
                full = self.BASE_URL.rstrip("/") + path
                videos.append(VideoRef(
                    site=self.NAME, video_id=vid, video_url=full, performer=username,
                ))
                if limit and len(videos) >= limit:
                    return videos
            if new == 0:
                break
            time.sleep(0.3)
        return videos

    def extract_stream(self, video: VideoRef) -> bool:
        try:
            r = self.session.get(video.video_url, timeout=20)
        except Exception as e:
            self.log.debug(f"  [{self.NAME}] fetch: {e}")
            return False
        if r.status_code != 200:
            return False
        html = r.text

        mt = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
        if mt:
            video.title = _html.unescape(mt.group(1))

        # Find vidello iframe — could be direct or two-hop via camcaps.io/embed
        vidello_url = None
        # Direct vidello iframe?
        m = re.search(r'<iframe[^>]+src="(https?://vidello\.[^"]+)"', html)
        if m:
            vidello_url = m.group(1)
        else:
            # Two-hop: camcaps embed first
            em = re.search(r'<iframe[^>]+src="(https?://camcaps\.[^"]+/embed/[^"]+)"', html)
            if em:
                try:
                    er = self.session.get(em.group(1), timeout=20,
                                          headers={"Referer": video.video_url})
                    if er.status_code == 200:
                        m2 = re.search(r'<iframe[^>]+src="(https?://vidello\.[^"]+)"', er.text)
                        if m2:
                            vidello_url = m2.group(1)
                except Exception:
                    pass

        if not vidello_url:
            self.log.debug(f"  [{self.NAME}] no vidello iframe found")
            return False

        # Fetch vidello embed
        try:
            vr = self.session.get(vidello_url, timeout=20,
                                  headers={"Referer": self.BASE_URL + "/"})
        except Exception as e:
            self.log.debug(f"  [{self.NAME}] vidello fetch: {e}")
            return False
        if vr.status_code != 200:
            return False

        # Extract sources: [{file: "...m3u8"}]
        ms = re.search(r"file:\s*[\"'](https?://[^\"']+\.m3u8[^\"']*)[\"']", vr.text)
        if not ms:
            ms = re.search(r"sources\s*:\s*\[\s*\{[^}]*file\s*:\s*[\"']([^\"']+)[\"']", vr.text)
        if not ms:
            ms = re.search(r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)', vr.text)

        if not ms:
            self.log.debug(f"  [{self.NAME}] no m3u8 in vidello page")
            return False

        video.stream_url = ms.group(1)
        video.stream_kind = "hls"
        video.stream_headers = {
            "Referer": "https://vidello.net/",
            "User-Agent": USER_AGENT,
        }
        return True


# ── camsrip.com (base64-encoded IDs, multi-platform aggregator) ──────────

class Camsrip(SiteScraper):
    """camsrip.com — archives Stripchat/Chaturbate/Camsoda/Bongacams/Cam4.
    Video IDs are base64-encoded numeric IDs. Profile URL: /{user}/profile."""
    NAME = "camsrip"
    BASE_URL = "https://camsrip.com"
    USE_CLOUDSCRAPER = True
    MIN_ENTRIES = 1

    NOT_FOUND_MARKERS = ["/notfound/", "/404/", "/error/", "server error"]
    VIDEO_LINK_RE = re.compile(r'href="(/watch/([A-Za-z0-9+/=]+))"')

    def _is_valid(self, requested: str, r: requests.Response) -> bool:
        if r.status_code != 200:
            return False
        final = r.url.lower()
        for m in self.NOT_FOUND_MARKERS:
            if m in final:
                return False
        if len(r.text) < 500:
            return False
        # Check we're still on the profile page
        if "/profile" not in final and "profile" not in r.text.lower():
            return False
        return True

    def probe(self, username: str) -> Optional[ProbeHit]:
        url = f"{self.BASE_URL}/{username}/profile"
        try:
            r = self.session.get(url, timeout=20)
        except Exception as e:
            self.log.debug(f"  [{self.NAME}] probe: {e}")
            return None
        if not self._is_valid(url, r):
            return None
        matches = self.VIDEO_LINK_RE.findall(r.text)
        unique = {vid for _, vid in matches}
        if len(unique) >= self.MIN_ENTRIES:
            return ProbeHit(site=self.NAME, url=url, entry_count=len(unique))
        return None

    def enumerate(self, hit: ProbeHit, username: str, limit: int) -> List[VideoRef]:
        videos: List[VideoRef] = []
        seen: set = set()
        for page in range(1, 20):
            url = hit.url if page == 1 else f"{hit.url}?page={page}"
            try:
                r = self.session.get(url, timeout=20)
            except Exception:
                break
            if r.status_code != 200:
                break
            matches = self.VIDEO_LINK_RE.findall(r.text)
            new = 0
            for path, vid_b64 in matches:
                if vid_b64 in seen:
                    continue
                seen.add(vid_b64)
                new += 1
                full = self.BASE_URL + path
                videos.append(VideoRef(
                    site=self.NAME,
                    video_id=vid_b64.rstrip("="),  # cleaner ID
                    video_url=full,
                    performer=username,
                ))
                if limit and len(videos) >= limit:
                    return videos
            if new == 0:
                break
            time.sleep(0.3)
        return videos

    def extract_stream(self, video: VideoRef) -> bool:
        """Fetch watch page, find stream URL (HLS or MP4)."""
        try:
            r = self.session.get(video.video_url, timeout=20)
        except Exception as e:
            self.log.debug(f"  [{self.NAME}] stream fetch: {e}")
            return False
        if r.status_code != 200:
            return False
        html = r.text

        # Title
        mt = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
        if mt:
            video.title = _html.unescape(mt.group(1))

        # Look for various stream sources
        for pat in [
            r'file:\s*["\'](https?://[^"\']+\.m3u8[^"\']*)["\']',
            r'src:\s*["\'](https?://[^"\']+\.m3u8[^"\']*)["\']',
            r'source\s+src="(https?://[^"]+\.m3u8[^"]*)"',
            r'"(https?://[^"]+\.m3u8[^"]*)"',
            r'file:\s*["\'](https?://[^"\']+\.mp4[^"\']*)["\']',
            r'source\s+src="(https?://[^"]+\.mp4[^"]*)"',
        ]:
            m = re.search(pat, html)
            if m:
                url = m.group(1)
                video.stream_url = url
                video.stream_kind = "hls" if ".m3u8" in url else "mp4"
                video.stream_headers = {
                    "Referer": video.video_url,
                    "User-Agent": USER_AGENT,
                }
                return True
        # Look for iframe embed
        m = re.search(r'<iframe[^>]+src="(https?://[^"]+)"', html)
        if m:
            iurl = m.group(1)
            try:
                ir = self.session.get(iurl, timeout=20, headers={"Referer": video.video_url})
                for pat in [
                    r'file:\s*["\'](https?://[^"\']+\.m3u8[^"\']*)["\']',
                    r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)',
                    r'(https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*)',
                ]:
                    m2 = re.search(pat, ir.text)
                    if m2:
                        url = m2.group(1)
                        video.stream_url = url
                        video.stream_kind = "hls" if ".m3u8" in url else "mp4"
                        video.stream_headers = {"Referer": iurl, "User-Agent": USER_AGENT}
                        return True
            except Exception:
                pass
        return False


# ── Coomer.st (OnlyFans/Fansly/CandFans mirror, NO auth) ────────────────────

class CoomerKemonoBase(SiteScraper):
    """Shared base for coomer.st and kemono.cr — same API shape.
    Critical: server requires Accept: text/css to bypass DDoS-Guard.

    Kemono's profile endpoint returns post_count=0 when queried by display-name;
    the real count only comes back when the numeric id is used. We therefore
    cache the creators catalog in memory and resolve names → ids lazily.
    """
    CATEGORY = "adult"
    MIN_ENTRIES = 1
    # List of services this mirror supports (e.g. onlyfans, fansly for coomer)
    SERVICES: List[str] = []
    # When True, resolve username -> numeric id via /api/v1/creators before probing.
    # Required on kemono; unnecessary on coomer (profile-by-name returns real count).
    RESOLVE_VIA_CATALOG: bool = False

    # Class-level catalog cache (shared across instances).
    _catalog_cache: Dict[str, List[dict]] = {}
    _catalog_lock_key = object()

    def _make_session(self) -> requests.Session:
        s = requests.Session()
        # DDoS-Guard bypass: server instruction is to send Accept: text/css
        s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/css",
            "Accept-Encoding": "gzip",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": self.BASE_URL + "/",
        })
        return s

    def _load_catalog(self) -> List[dict]:
        """Fetch /api/v1/creators (large JSON) once and cache it."""
        cache_key = self.BASE_URL
        if cache_key in self._catalog_cache:
            return self._catalog_cache[cache_key]
        try:
            r = self.session.get(f"{self.BASE_URL}/api/v1/creators", timeout=60)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    self._catalog_cache[cache_key] = data
                    self.log.debug(f"  [{self.NAME}] cached {len(data)} creators from catalog")
                    return data
        except Exception as e:
            self.log.debug(f"  [{self.NAME}] catalog fetch failed: {e}")
        self._catalog_cache[cache_key] = []
        return []

    def _resolve_via_catalog(self, username: str, service: str) -> Optional[str]:
        """Look up a creator in the catalog. Returns their numeric id or None.
        Matches case-insensitively on `name` or `public_id`."""
        target = username.strip().lower()
        for c in self._load_catalog():
            if c.get("service") != service:
                continue
            if (str(c.get("name", "")).strip().lower() == target
                    or str(c.get("public_id", "")).strip().lower() == target):
                return str(c.get("id"))
        return None

    def probe(self, username: str) -> Optional[ProbeHit]:
        """Probe each service for this username. Returns the one with most posts."""
        best: Optional[ProbeHit] = None
        for service in self.SERVICES:
            # Step 1: figure out which id to query. For coomer, the username
            # itself is a valid id. For kemono we must resolve via catalog.
            query_ids: List[str] = [username]
            if self.RESOLVE_VIA_CATALOG:
                resolved = self._resolve_via_catalog(username, service)
                if resolved:
                    query_ids = [resolved]
                else:
                    # Catalog miss — username probably doesn't exist on this service
                    continue

            for qid in query_ids:
                try:
                    url = f"{self.BASE_URL}/api/v1/{service}/user/{qid}/profile"
                    r = self.session.get(url, timeout=15)
                except Exception:
                    continue
                if r.status_code != 200:
                    continue
                try:
                    data = r.json()
                except Exception:
                    continue
                if not isinstance(data, dict) or "id" not in data:
                    continue
                post_count = int(data.get("post_count", 0) or 0)
                if post_count < self.MIN_ENTRIES:
                    continue
                display_url = f"{self.BASE_URL}/{service}/user/{username}"
                if best is None or post_count > best.entry_count:
                    best = ProbeHit(site=self.NAME, url=display_url, entry_count=post_count,
                                    uploader_id=f"{service}|{data.get('id', qid)}")
        return best

    def enumerate(self, hit: ProbeHit, username: str, limit: int) -> List[VideoRef]:
        # Extract service and user id from hit.uploader_id
        if "|" in hit.uploader_id:
            service, user_id = hit.uploader_id.split("|", 1)
        else:
            # Fallback: parse from hit.url
            m = re.search(r"/(\w+)/user/([^/]+)", hit.url)
            if not m:
                return []
            service, user_id = m.group(1), m.group(2)

        videos: List[VideoRef] = []
        seen: set = set()
        offset = 0
        while True:
            url = f"{self.BASE_URL}/api/v1/{service}/user/{user_id}/posts?o={offset}"
            try:
                r = self.session.get(url, timeout=20)
            except Exception:
                break
            if r.status_code != 200:
                break
            try:
                posts = r.json()
            except Exception:
                break
            if not posts:
                break
            for post in posts:
                post_id = post.get("id")
                if not post_id or post_id in seen:
                    continue
                seen.add(post_id)
                # Check for video attachments
                # The file field and attachments[] may contain videos
                attachments = []
                if post.get("file") and isinstance(post["file"], dict):
                    attachments.append(post["file"])
                attachments.extend(post.get("attachments", []) or [])
                for att in attachments:
                    if not att.get("path"):
                        continue
                    name = att.get("name", "")
                    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                    if ext not in ("mp4", "mkv", "webm", "mov", "m4v"):
                        continue
                    media_id = f"{post_id}_{att.get('path', '').split('/')[-1].split('.')[0][:12]}"
                    title = post.get("title", "") or name
                    videos.append(VideoRef(
                        site=self.NAME,
                        video_id=media_id,
                        video_url=self.BASE_URL + "/data" + att["path"],
                        title=title,
                        performer=username,
                        uploader_id=service,
                    ))
                    if limit and len(videos) >= limit:
                        return videos
            if len(posts) < 50:
                break
            offset += 50
            time.sleep(0.8)  # polite — DDoS-Guard is sensitive
        return videos

    def extract_stream(self, video: VideoRef) -> bool:
        """Coomer/Kemono URLs are already direct — just set up download headers."""
        if not video.video_url:
            return False
        video.stream_url = video.video_url
        video.stream_kind = "mp4"
        video.stream_headers = {
            "User-Agent": USER_AGENT,
            "Referer": self.BASE_URL + "/",
            "Accept": "*/*",
        }
        return True


class Coomer(CoomerKemonoBase):
    """coomer.st — OnlyFans/Fansly/CandFans leak mirror."""
    NAME = "coomer"
    BASE_URL = "https://coomer.st"
    SERVICES = ["onlyfans", "fansly", "candfans"]
    COOKIE_DOMAIN = "coomer.st"


class Kemono(CoomerKemonoBase):
    """kemono.cr — Patreon/Fanbox/Gumroad/SubscribeStar/Fantia/Boosty/Discord mirror.

    Kemono's profile-by-name endpoint returns post_count=0 — we must resolve
    the display name to the numeric Patreon/Fanbox id via the creators catalog.
    """
    NAME = "kemono"
    BASE_URL = "https://kemono.cr"
    SERVICES = ["patreon", "fanbox", "gumroad", "subscribestar", "fantia", "boosty", "discord", "dlsite"]
    COOKIE_DOMAIN = "kemono.cr"
    RESOLVE_VIA_CATALOG = True


# ── RedGifs (user profile API, NO auth) ─────────────────────────────────────

class RedGifs(SiteScraper):
    """RedGifs — mostly used for Reddit-linked video content. Has clean v2 API."""
    NAME = "redgifs"
    BASE_URL = "https://api.redgifs.com"
    CATEGORY = "adult"
    MIN_ENTRIES = 1

    def _get_token(self) -> Optional[str]:
        """RedGifs requires a guest temporary token obtained via /v2/auth/temporary."""
        try:
            r = self.session.get(f"{self.BASE_URL}/v2/auth/temporary", timeout=15)
            if r.status_code == 200:
                data = r.json()
                token = data.get("token")
                if token:
                    self.session.headers["Authorization"] = f"Bearer {token}"
                    return token
        except Exception as e:
            self.log.debug(f"  [{self.NAME}] token fetch: {e}")
        return None

    def probe(self, username: str) -> Optional[ProbeHit]:
        if "Authorization" not in self.session.headers:
            if not self._get_token():
                return None
        # RedGifs deprecated /v2/users/{u} for profiles; /v1/users/{u} still works.
        url = f"{self.BASE_URL}/v1/users/{username.lower()}"
        try:
            r = self.session.get(url, timeout=15)
        except Exception as e:
            self.log.debug(f"  [{self.NAME}] probe: {e}")
            return None
        if r.status_code == 401:
            self._get_token()
            try:
                r = self.session.get(url, timeout=15)
            except Exception:
                return None
        if r.status_code != 200:
            return None
        try:
            data = r.json()
        except Exception:
            return None
        count = int(data.get("publishedGifs", data.get("gifs", 0)) or 0)
        if count < self.MIN_ENTRIES:
            return None
        return ProbeHit(site=self.NAME, url=f"https://www.redgifs.com/users/{username}",
                        entry_count=count, uploader_id=username.lower())

    def enumerate(self, hit: ProbeHit, username: str, limit: int) -> List[VideoRef]:
        if "Authorization" not in self.session.headers:
            if not self._get_token():
                return []
        videos: List[VideoRef] = []
        seen: set = set()
        page = 1
        username_lower = hit.uploader_id or username.lower()
        while True:
            url = f"{self.BASE_URL}/v2/users/{username_lower}/search"
            try:
                r = self.session.get(url, params={"page": page, "count": 80, "order": "best"}, timeout=20)
            except Exception:
                break
            if r.status_code != 200:
                break
            try:
                data = r.json()
            except Exception:
                break
            gifs = data.get("gifs", [])
            if not gifs:
                break
            for g in gifs:
                gid = g.get("id")
                if not gid or gid in seen:
                    continue
                seen.add(gid)
                urls = g.get("urls", {})
                # Prefer hd over sd
                stream = urls.get("hd") or urls.get("sd") or urls.get("vthumbnail")
                if not stream:
                    continue
                videos.append(VideoRef(
                    site=self.NAME,
                    video_id=gid,
                    video_url=stream,
                    title=g.get("description", "") or gid,
                    duration=g.get("duration", 0) or 0,
                    performer=username,
                ))
                # We can skip the extract_stream step — URL is already direct
                videos[-1].stream_url = stream
                videos[-1].stream_kind = "mp4"
                videos[-1].stream_headers = {
                    "User-Agent": USER_AGENT,
                    "Referer": "https://www.redgifs.com/",
                }
                if limit and len(videos) >= limit:
                    return videos
            pages = data.get("pages", 1)
            if page >= pages:
                break
            page += 1
            time.sleep(0.5)
        return videos

    def extract_stream(self, video: VideoRef) -> bool:
        # Already populated during enumerate
        if video.stream_url:
            return True
        # Fallback: fetch single gif by ID
        gid = video.video_id
        if "Authorization" not in self.session.headers:
            self._get_token()
        try:
            r = self.session.get(f"{self.BASE_URL}/v2/gifs/{gid}", timeout=15)
        except Exception:
            return False
        if r.status_code != 200:
            return False
        try:
            data = r.json()
        except Exception:
            return False
        urls = (data.get("gif") or {}).get("urls", {})
        stream = urls.get("hd") or urls.get("sd")
        if not stream:
            return False
        video.stream_url = stream
        video.stream_kind = "mp4"
        video.stream_headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://www.redgifs.com/",
        }
        return True


# ── X.com / Twitter (requires auth cookies for reliable access) ──────────────

class XCom(SiteScraper):
    """X.com (Twitter) user video scraper. Uses the private GraphQL API with
    auth cookies (auth_token + ct0). Premium accounts work best — higher rate
    limits and access to long videos."""
    NAME = "xcom"
    BASE_URL = "https://x.com"
    CATEGORY = "mainstream"
    USE_CLOUDSCRAPER = False
    MIN_ENTRIES = 1
    COOKIE_DOMAIN = "x.com"

    BEARER = ("Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejR"
              "COuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA")
    # Query IDs — if these rotate, scraper must update. Known working 2026.
    QID_USER_BY_NAME = ["ck5KkZ8t5cOmoLssopN99Q", "1VOOyvKkiI3FMmkeDNxM9A"]
    QID_USER_MEDIA = ["jCRhbOzdgOHp6u9H4g2tEg", "vFPc2LVIu7so2uA_gHQAdg"]

    def _make_session(self) -> requests.Session:
        s = super()._make_session()
        ct0 = ""
        if self._cookie_jar:
            for c in self._cookie_jar:
                if "x.com" in c.domain or "twitter.com" in c.domain:
                    if c.name == "ct0":
                        ct0 = c.value
                        break
        s.headers.update({
            "authorization": self.BEARER,
            "x-csrf-token": ct0,
            "x-twitter-auth-type": "OAuth2Session" if ct0 else "",
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
            "content-type": "application/json",
            "Referer": "https://x.com/",
        })
        if not ct0:
            del s.headers["x-twitter-auth-type"]
        return s

    def _check_auth(self) -> bool:
        if not self._cookie_jar:
            return False
        names = {c.name for c in self._cookie_jar if ("x.com" in c.domain or "twitter.com" in c.domain)}
        return "auth_token" in names and "ct0" in names

    def _gql_get(self, qid_list: List[str], op_name: str, variables: dict,
                 features: Optional[dict] = None) -> Optional[dict]:
        features = features or {
            "rweb_tipjar_consumption_enabled": True,
            "responsive_web_graphql_exclude_directive_enabled": True,
            "verified_phone_label_enabled": False,
            "creator_subscriptions_tweet_preview_api_enabled": True,
            "responsive_web_graphql_timeline_navigation_enabled": True,
            "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
            "communities_web_enable_tweet_community_results_fetch": True,
            "c9s_tweet_anatomy_moderator_badge_enabled": True,
            "articles_preview_enabled": True,
            "tweetypie_unmention_optimization_enabled": True,
            "responsive_web_edit_tweet_api_enabled": True,
            "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
            "view_counts_everywhere_api_enabled": True,
            "longform_notetweets_consumption_enabled": True,
            "responsive_web_twitter_article_tweet_consumption_enabled": True,
            "tweet_awards_web_tipping_enabled": False,
            "creator_subscriptions_quote_tweet_preview_enabled": False,
            "freedom_of_speech_not_reach_fetch_enabled": True,
            "standardized_nudges_misinfo": True,
            "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
            "rweb_video_timestamps_enabled": True,
            "longform_notetweets_rich_text_read_enabled": True,
            "longform_notetweets_inline_media_enabled": True,
            "responsive_web_enhance_cards_enabled": False,
        }
        params = {
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": json.dumps(features, separators=(",", ":")),
        }
        for qid in qid_list:
            url = f"{self.BASE_URL}/i/api/graphql/{qid}/{op_name}"
            for attempt in range(3):
                try:
                    r = self.session.get(url, params=params, timeout=30)
                except Exception as e:
                    self.log.debug(f"  [{self.NAME}] {op_name}: {e}")
                    break
                if r.status_code == 200:
                    try:
                        return r.json()
                    except Exception:
                        return None
                if r.status_code == 429:
                    reset = int(r.headers.get("x-rate-limit-reset", 0))
                    wait = max(30, reset - int(time.time())) if reset else 60
                    self.log.debug(f"  [{self.NAME}] rate limited, waiting {wait}s")
                    time.sleep(min(wait, 300))
                    continue
                if r.status_code in (400, 401, 404):
                    break  # try next qid
        return None

    def probe(self, username: str) -> Optional[ProbeHit]:
        if not self._check_auth():
            self.log.debug(f"  [{self.NAME}] no auth cookies — skipping")
            return None
        data = self._gql_get(
            self.QID_USER_BY_NAME, "UserByScreenName",
            variables={"screen_name": username, "withSafetyModeUserFields": True},
        )
        if not data:
            return None
        try:
            result = data["data"]["user"]["result"]
            if result.get("__typename") == "UserUnavailable":
                return None
            user_id = result["rest_id"]
            # media_count is on legacy
            media_count = int(result.get("legacy", {}).get("media_count", 0))
        except (KeyError, TypeError):
            return None
        if media_count < 1:
            return None
        return ProbeHit(site=self.NAME, url=f"https://x.com/{username}/media",
                        entry_count=media_count, uploader_id=user_id)

    def _walk_tweets(self, node):
        """Recursively yield Tweet objects from a nested entry."""
        if isinstance(node, dict):
            tt = node.get("__typename")
            if tt == "Tweet":
                yield node
            elif tt == "TweetWithVisibilityResults":
                inner = node.get("tweet")
                if inner:
                    yield inner
            for v in node.values():
                yield from self._walk_tweets(v)
        elif isinstance(node, list):
            for v in node:
                yield from self._walk_tweets(v)

    def enumerate(self, hit: ProbeHit, username: str, limit: int) -> List[VideoRef]:
        user_id = hit.uploader_id
        if not user_id:
            return []
        videos: List[VideoRef] = []
        seen_media: set = set()
        cursor = None
        for _ in range(50):  # hard cap on pages
            variables = {
                "userId": user_id,
                "count": 100,
                "includePromotedContent": False,
                "withClientEventToken": False,
                "withBirdwatchNotes": False,
                "withVoice": True,
                "withV2Timeline": True,
            }
            if cursor:
                variables["cursor"] = cursor
            data = self._gql_get(
                self.QID_USER_MEDIA, "UserMedia", variables=variables,
            )
            if not data:
                break
            try:
                instructions = (
                    data["data"]["user"]["result"]
                    ["timeline_v2"]["timeline"]["instructions"]
                )
            except (KeyError, TypeError):
                break
            new_count = 0
            next_cursor = None
            for inst in instructions:
                if inst.get("type") not in ("TimelineAddEntries", "TimelineAddToModule"):
                    continue
                for entry in (inst.get("entries") or inst.get("moduleItems") or []):
                    entry_id = entry.get("entryId", "")
                    content = entry.get("content") or entry.get("item", {}).get("itemContent", {})
                    if content.get("entryType") == "TimelineTimelineCursor" or "cursor-bottom" in entry_id:
                        if content.get("cursorType") == "Bottom" or "cursor-bottom" in entry_id:
                            next_cursor = content.get("value")
                        continue
                    for tweet in self._walk_tweets(content):
                        tid = tweet.get("rest_id") or tweet.get("legacy", {}).get("id_str")
                        legacy = tweet.get("legacy", {})
                        media_list = legacy.get("extended_entities", {}).get("media", [])
                        for media in media_list:
                            if media.get("type") not in ("video", "animated_gif"):
                                continue
                            media_id = media.get("id_str") or f"{tid}_{len(videos)}"
                            if media_id in seen_media:
                                continue
                            seen_media.add(media_id)
                            # Pick best variant
                            variants = (media.get("video_info") or {}).get("variants", [])
                            mp4s = [v for v in variants if v.get("content_type") == "video/mp4" and v.get("bitrate")]
                            hls = [v for v in variants if v.get("content_type") == "application/x-mpegURL"]
                            chosen = max(mp4s, key=lambda v: v["bitrate"]) if mp4s else (hls[0] if hls else None)
                            if not chosen:
                                continue
                            title = (legacy.get("full_text", "")[:80] or media_id).replace("\n", " ")
                            vr = VideoRef(
                                site=self.NAME,
                                video_id=f"{tid}_{media_id}",
                                video_url=f"https://x.com/{username}/status/{tid}",
                                title=title,
                                performer=username,
                                duration=(media.get("video_info") or {}).get("duration_millis", 0) / 1000.0,
                            )
                            vr.stream_url = chosen["url"]
                            vr.stream_kind = "hls" if chosen["content_type"] == "application/x-mpegURL" else "mp4"
                            vr.stream_headers = {
                                "User-Agent": USER_AGENT,
                                "Referer": "https://x.com/",
                            }
                            videos.append(vr)
                            new_count += 1
                            if limit and len(videos) >= limit:
                                return videos
            if not next_cursor or new_count == 0:
                break
            cursor = next_cursor
            time.sleep(1.5)  # avoid rate limiting
        return videos

    def extract_stream(self, video: VideoRef) -> bool:
        # Already populated during enumerate
        return bool(video.stream_url)


# ── Reddit (user's submitted videos via .json API) ──────────────────────────

class RedditUser(SiteScraper):
    """Reddit user's video submissions. Uses the public .json API (no auth needed)."""
    NAME = "reddit"
    BASE_URL = "https://www.reddit.com"
    CATEGORY = "mainstream"
    MIN_ENTRIES = 1

    def _make_session(self) -> requests.Session:
        s = super()._make_session()
        # Reddit's .json API blocks Accept: text/html — it serves the HTML page
        # when it detects browser-ish headers. Needs application/json or */*.
        s.headers["User-Agent"] = USER_AGENT
        s.headers["Accept"] = "application/json, */*"
        # Reddit's bot detector fires a 403 when *only* Accept-Language: en-US,en;q=0.9
        # is present (i.e. the minimal browser-y signature with a specific UA).
        # Drop it — Reddit ignores missing Accept-Language just fine.
        s.headers.pop("Accept-Language", None)
        return s

    def probe(self, username: str) -> Optional[ProbeHit]:
        url = f"{self.BASE_URL}/user/{username}/submitted.json?limit=100"
        try:
            r = self.session.get(url, timeout=15)
        except Exception:
            return None
        if r.status_code != 200:
            return None
        try:
            data = r.json()
        except Exception:
            return None
        children = (data.get("data") or {}).get("children", [])
        video_count = sum(
            1 for c in children
            if (c.get("data") or {}).get("is_video") or
               "v.redd.it" in str((c.get("data") or {}).get("url", "")) or
               "redgifs.com" in str((c.get("data") or {}).get("url", ""))
        )
        if video_count < 1:
            return None
        return ProbeHit(site=self.NAME, url=f"https://www.reddit.com/user/{username}/submitted/",
                        entry_count=video_count, uploader_id=username)

    def enumerate(self, hit: ProbeHit, username: str, limit: int) -> List[VideoRef]:
        videos: List[VideoRef] = []
        seen: set = set()
        after = None
        for _ in range(20):  # max 20 pages × 100 = 2000 submissions
            url = f"{self.BASE_URL}/user/{username}/submitted.json?limit=100"
            if after:
                url += f"&after={after}"
            try:
                r = self.session.get(url, timeout=15)
            except Exception:
                break
            if r.status_code != 200:
                break
            try:
                data = r.json()
            except Exception:
                break
            children = (data.get("data") or {}).get("children", [])
            if not children:
                break
            for c in children:
                post = c.get("data") or {}
                post_id = post.get("id")
                if not post_id or post_id in seen:
                    continue
                seen.add(post_id)
                video_url = None
                is_hls = False
                # v.redd.it video (Reddit's own hosting)
                if post.get("is_video"):
                    media = post.get("media", {})
                    rv = media.get("reddit_video") if media else None
                    if rv:
                        video_url = rv.get("fallback_url") or rv.get("hls_url")
                        if rv.get("hls_url") and ".m3u8" in (rv.get("hls_url") or ""):
                            is_hls = "hls_url" in rv and video_url == rv.get("hls_url")
                # RedGifs, GfyCat links
                elif "redgifs.com" in str(post.get("url", "")):
                    # Let RedGifs scraper handle if we can; otherwise skip
                    continue
                # Direct video URL in post.url
                elif any(ext in str(post.get("url", "")) for ext in [".mp4", ".webm", ".mov"]):
                    video_url = post.get("url")
                if not video_url:
                    continue
                title = post.get("title", "")[:120] or post_id
                vr = VideoRef(
                    site=self.NAME,
                    video_id=post_id,
                    video_url=f"https://www.reddit.com{post.get('permalink', '')}",
                    title=title,
                    performer=username,
                )
                vr.stream_url = video_url
                vr.stream_kind = "hls" if ".m3u8" in video_url or is_hls else "mp4"
                vr.stream_headers = {
                    "User-Agent": USER_AGENT,
                    "Referer": "https://www.reddit.com/",
                }
                videos.append(vr)
                if limit and len(videos) >= limit:
                    return videos
            after = (data.get("data") or {}).get("after")
            if not after:
                break
            time.sleep(1.0)
        return videos

    def extract_stream(self, video: VideoRef) -> bool:
        return bool(video.stream_url)


# ── Recu.me (requires browser cookies — cf_clearance + optional premium) ──

class Recume(SiteScraper):
    """Recu.me — Chaturbate recordings archive. Requires authenticated cookies
    (cf_clearance from a browser session, plus optional premium login cookie
    for unlimited plays).

    Setup: user must export browser cookies for recu.me into cookies.txt and
    point `cookies_file` at it. Free tier allows ~1-5 plays/day; premium is
    unlimited. Without cookies, every request returns Cloudflare challenge."""
    NAME = "recume"
    BASE_URL = "https://recu.me"
    CATEGORY = "adult"
    USE_CLOUDSCRAPER = True
    MIN_ENTRIES = 1
    COOKIE_DOMAIN = "recu.me"

    VIDEO_LINK_RE = re.compile(r'href="(/(?:[\w.-]+/)?video/(\d+)/play)"')
    PLAY_TOKEN_RE = re.compile(
        r'id="play_button"[^>]*data-video-id="(?P<vid>\d+)"[^>]*data-token="(?P<token>[^"]+)"',
        re.DOTALL,
    )
    # Alternative: find token after the video ID appears
    PLAY_TOKEN_RE2 = re.compile(
        r'data-video-id="(?P<vid>\d+)"[^>]*data-token="(?P<token>[^"]+)"',
        re.DOTALL,
    )

    def _is_blocked(self, r: requests.Response) -> bool:
        """Detect Cloudflare challenge or missing cookies."""
        body = r.text[:2000].lower()
        if r.status_code == 403:
            return True
        if "just a moment" in body and "challenge" in body:
            return True
        if "cf_clearance" in body and "cloudflare" in body:
            return True
        if "/account/signin" in r.url:
            return True
        return False

    def _check_cookies(self) -> bool:
        """Verify cookies contain cf_clearance or im18."""
        if not self._cookie_jar:
            return False
        names = [c.name for c in self._cookie_jar if self.COOKIE_DOMAIN in c.domain]
        return "cf_clearance" in names

    def probe(self, username: str) -> Optional[ProbeHit]:
        if not self._check_cookies():
            self.log.debug(f"  [{self.NAME}] no cf_clearance cookie — skipping")
            return None
        url = f"{self.BASE_URL}/performer/{username}"
        try:
            r = self.session.get(url, timeout=20)
        except Exception as e:
            self.log.debug(f"  [{self.NAME}] probe: {e}")
            return None
        if r.status_code != 200 or self._is_blocked(r):
            self.log.debug(f"  [{self.NAME}] probe blocked or {r.status_code}")
            return None
        matches = self.VIDEO_LINK_RE.findall(r.text)
        unique = {vid for _, vid in matches}
        if not unique:
            return None
        return ProbeHit(site=self.NAME, url=url, entry_count=len(unique))

    def enumerate(self, hit: ProbeHit, username: str, limit: int) -> List[VideoRef]:
        videos: List[VideoRef] = []
        seen: set = set()
        for page in range(1, 50):
            url = hit.url if page == 1 else f"{hit.url}?sort=date&page={page}"
            try:
                r = self.session.get(url, timeout=20)
            except Exception:
                break
            if r.status_code != 200 or self._is_blocked(r):
                break
            matches = self.VIDEO_LINK_RE.findall(r.text)
            new = 0
            for path, vid in matches:
                if vid in seen:
                    continue
                seen.add(vid)
                new += 1
                full = self.BASE_URL + path
                videos.append(VideoRef(
                    site=self.NAME, video_id=vid, video_url=full, performer=username,
                ))
                if limit and len(videos) >= limit:
                    return videos
            if new == 0:
                break
            time.sleep(1.0)
        return videos

    def _sign_ts_url(self, url: str) -> str:
        """Append &check=... signature to a .ts segment URL.
        Algorithm from baconator696/Recu-Download: check = req[:4] + uid[2:6] + expires[-4:]"""
        uid_m = re.search(r'uid=([^&]*)', url)
        exp_m = re.search(r'expires=([^&]*)', url)
        req_m = re.search(r'request_id=([^&]*)', url)
        if not (uid_m and exp_m and req_m):
            return url
        check = req_m.group(1)[:4] + uid_m.group(1)[2:6] + exp_m.group(1)[-4:]
        return f"{url}&check={check}"

    def extract_stream(self, video: VideoRef) -> bool:
        try:
            r = self.session.get(video.video_url, timeout=20)
        except Exception as e:
            self.log.debug(f"  [{self.NAME}] stream fetch: {e}")
            return False
        if r.status_code != 200:
            self.log.debug(f"  [{self.NAME}] HTTP {r.status_code}")
            return False
        if self._is_blocked(r):
            self.log.warning(f"  [{self.NAME}] blocked by Cloudflare — cf_clearance expired?")
            return False
        html = r.text

        # Title
        mt = re.search(r'<title>([^<]+)</title>', html)
        if mt:
            video.title = _html.unescape(mt.group(1).strip())

        # Find token
        m = self.PLAY_TOKEN_RE.search(html) or self.PLAY_TOKEN_RE2.search(html)
        if not m:
            # Fallback: any data-token
            m2 = re.search(r'data-token="([^"]+)"', html)
            if not m2:
                self.log.debug(f"  [{self.NAME}] no data-token in video page")
                return False
            token = m2.group(1)
            vid = video.video_id
        else:
            token = m.group("token")
            vid = m.group("vid")

        # Call API
        api_url = f"{self.BASE_URL}/api/video/{vid}?token={urllib.parse.quote(token, safe='&=-')}"
        try:
            ar = self.session.get(api_url, timeout=15, headers={
                "Referer": video.video_url,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "*/*",
            })
        except Exception as e:
            self.log.debug(f"  [{self.NAME}] API fetch: {e}")
            return False
        body = ar.text.strip()

        # Sentinel responses
        if body == "shall_subscribe":
            self.log.info(f"  [{self.NAME}] {vid}: daily free quota exhausted (needs premium)")
            video.stream_kind = "private"  # mark permanent-skip
            return False
        if body == "shall_signin":
            self.log.info(f"  [{self.NAME}] {vid}: not signed in (cookies expired?)")
            return False
        if body == "wrong_token":
            self.log.debug(f"  [{self.NAME}] {vid}: token expired (will retry)")
            return False

        # Body is an HTML fragment with <source src="m3u8"> or <video src="mp4">
        ms = re.search(r'<source\s+src="([^"]+)"', body)
        if not ms:
            ms = re.search(r'<video[^>]+src="([^"]+)"', body)
        if not ms:
            self.log.debug(f"  [{self.NAME}] no media source in API response")
            return False

        stream_url = ms.group(1).replace("&amp;", "&")
        video.stream_url = stream_url
        video.stream_kind = "hls" if ".m3u8" in stream_url else "mp4"
        video.stream_headers = {
            "Referer": video.video_url,
            "User-Agent": USER_AGENT,
            "Origin": self.BASE_URL,
        }
        return True


# ── CamSmut.com (VOE.sx video host + optional login) ─────────────────────────

import base64 as _base64


class CamSmut(SiteScraper):
    """camsmut.com — free-to-view cam archive site that hosts videos on
    VOE.sx. Video pages return 404 without a logged-in session cookie, so
    the first run performs an automatic login if username/password are
    supplied via class attributes (`USERNAME` / `PASSWORD`), env vars
    `CAMSMUT_USERNAME` / `CAMSMUT_PASSWORD`, or a project-level
    `camsmut_credentials.json`. Once logged in, the session cookie is
    cached and re-used.

    The page embeds VOE.sx via an iframe whose `data-src` attribute is the
    VOE URL reversed and base64-encoded. After decoding, we hand the VOE
    URL off to yt-dlp's built-in VoeIE extractor, which returns the m3u8
    stream URL.
    """

    NAME = "camsmut"
    BASE_URL = "https://camsmut.com"
    CATEGORY = "cam"
    MIN_ENTRIES = 1
    COOKIE_DOMAIN = "camsmut.com"

    # Optional credentials — set by the CLI / webui after reading config.
    USERNAME: str = ""
    PASSWORD: str = ""

    VIDEO_LINK_RE = re.compile(r'href="(/video/([a-z0-9]+)/([^"]+))"', re.IGNORECASE)
    PLAYER_DATA_SRC_RE = re.compile(
        r'id="player"[^>]*data-src="([^"]+)"', re.IGNORECASE)
    CSRF_RE = re.compile(
        r'<input[^>]+type="hidden"[^>]+value="([A-Za-z0-9]{16,})"', re.IGNORECASE)

    def _make_session(self) -> requests.Session:
        s = super()._make_session()
        # Skip age gate; harmless cookie.
        s.cookies.set("access", "1", domain="camsmut.com", path="/")
        self._logged_in = False
        return s

    def _credentials(self) -> tuple[str, str]:
        """Resolve camsmut credentials from (in order): class attrs,
        CAMSMUT_* env vars, camsmut_credentials.json in script dir."""
        u, p = self.USERNAME, self.PASSWORD
        if not u or not p:
            u = u or os.environ.get("CAMSMUT_USERNAME", "")
            p = p or os.environ.get("CAMSMUT_PASSWORD", "")
        if not u or not p:
            try:
                creds_file = Path(__file__).with_name("camsmut_credentials.json")
                if creds_file.exists():
                    data = json.loads(creds_file.read_text(encoding="utf-8"))
                    u = u or data.get("username", "")
                    p = p or data.get("password", "")
            except Exception:
                pass
        return u, p

    def _login(self) -> bool:
        if self._logged_in:
            return True
        u, p = self._credentials()
        if not u or not p:
            self.log.debug(f"  [{self.NAME}] no credentials — skipping login")
            return False
        try:
            r = self.session.get(f"{self.BASE_URL}/login", timeout=15)
            if r.status_code != 200:
                return False
            csrf = ""
            m = self.CSRF_RE.search(r.text)
            if m:
                csrf = m.group(1)
            # Find the actual field names
            csrf_field = "csrf"
            for mm in re.finditer(r'<input[^>]+name="([^"]+)"[^>]+type="hidden"', r.text):
                csrf_field = mm.group(1)
            payload = {"username": u, "password": p, "remember": "on"}
            if csrf:
                payload[csrf_field] = csrf
            r2 = self.session.post(
                f"{self.BASE_URL}/login",
                data=payload, timeout=15, allow_redirects=True,
                headers={"Referer": f"{self.BASE_URL}/login"},
            )
            # Heuristic: if the login page is NOT in the final URL, we
            # probably succeeded. Verify by hitting any known-good page.
            self._logged_in = "/login" not in r2.url or r2.status_code == 200
            if self._logged_in:
                self.log.debug(f"  [{self.NAME}] login OK for {u}")
            else:
                self.log.warning(f"  [{self.NAME}] login failed for {u}")
            return self._logged_in
        except Exception as e:
            self.log.debug(f"  [{self.NAME}] login exception: {e}")
            return False

    def _has_session_cookie(self) -> bool:
        """Check if we already have a valid session cookie (from cookies.txt)."""
        for c in self.session.cookies:
            if "camsmut.com" in (c.domain or "") and "session" in c.name.lower():
                return True
        return False

    def _ensure_auth(self) -> None:
        """Login via credentials if cookies don't already provide a session."""
        if self._has_session_cookie():
            return
        self._login()

    @staticmethod
    def _decode_data_src(encoded: str) -> str:
        """atob(encoded.split('').reverse().join('')) — camsmut's VOE obfuscation."""
        try:
            return _base64.b64decode(encoded[::-1]).decode("utf-8")
        except Exception:
            return ""

    def probe(self, username: str) -> Optional[ProbeHit]:
        self._ensure_auth()
        try:
            r = self.session.get(
                f"{self.BASE_URL}/search",
                params={"q": username}, timeout=20,
            )
        except Exception as e:
            self.log.debug(f"  [{self.NAME}] probe: {e}")
            return None
        if r.status_code != 200:
            return None
        matches = self.VIDEO_LINK_RE.findall(r.text)
        if not matches:
            return None
        # Filter to links whose slug contains the username (reduces false positives
        # on cross-performer "similar videos" sections)
        u_lower = username.lower()
        filtered = [m for m in matches if u_lower in m[2].lower()]
        if not filtered:
            # Fall back to all matches if filter is too aggressive
            filtered = matches
        unique = {vhash for _, vhash, _ in filtered}
        if len(unique) < self.MIN_ENTRIES:
            return None
        return ProbeHit(
            site=self.NAME,
            url=f"{self.BASE_URL}/search?q={username}",
            entry_count=len(unique),
        )

    def enumerate(self, hit: ProbeHit, username: str, limit: int) -> List[VideoRef]:
        self._ensure_auth()
        videos: List[VideoRef] = []
        seen: set = set()
        u_lower = username.lower()
        for page in range(1, 50):
            url = hit.url if page == 1 else f"{hit.url}&page={page}"
            try:
                r = self.session.get(url, timeout=20)
            except Exception:
                break
            if r.status_code != 200:
                break
            matches = self.VIDEO_LINK_RE.findall(r.text)
            new_this_page = 0
            for path, vhash, slug in matches:
                if vhash in seen:
                    continue
                if u_lower not in slug.lower() and u_lower not in path.lower():
                    continue
                seen.add(vhash)
                new_this_page += 1
                videos.append(VideoRef(
                    site=self.NAME,
                    video_id=vhash,
                    video_url=f"{self.BASE_URL}{path}",
                    title=slug.replace("-", " "),
                    performer=username,
                ))
                if limit and len(videos) >= limit:
                    return videos
            if new_this_page == 0:
                break
            time.sleep(0.5)
        return videos

    def extract_stream(self, video: VideoRef) -> bool:
        """Resolve the VOE.sx stream URL for a single video.

        Flow: GET the camsmut video page -> decode the iframe's data-src ->
        yields the VOE embed URL. We then delegate to yt-dlp's built-in
        VoeIE extractor to resolve the m3u8 stream URL.
        """
        self._ensure_auth()
        try:
            r = self.session.get(video.video_url, timeout=20)
        except Exception as e:
            self.log.debug(f"  [{self.NAME}] page fetch: {e}")
            return False
        if r.status_code == 404:
            video.stream_kind = "private"
            return False
        if r.status_code != 200:
            return False
        m = self.PLAYER_DATA_SRC_RE.search(r.text)
        if not m:
            return False
        voe_url = self._decode_data_src(m.group(1))
        if not voe_url or "http" not in voe_url:
            return False

        # Delegate to yt-dlp's VoeIE for m3u8 resolution.
        try:
            import yt_dlp
            with yt_dlp.YoutubeDL({
                "quiet": True, "no_warnings": True,
                "skip_download": True,
                "extract_flat": False,
            }) as ydl:
                info = ydl.extract_info(voe_url, download=False)
                if not info:
                    return False
                # Pick the best format
                fmts = info.get("formats") or []
                hls = [f for f in fmts if (f.get("protocol") or "").startswith("m3u8")]
                mp4 = [f for f in fmts if f.get("ext") == "mp4" and f.get("url")]
                chosen = None
                if hls:
                    chosen = max(hls, key=lambda f: f.get("tbr") or f.get("height") or 0)
                elif mp4:
                    chosen = max(mp4, key=lambda f: f.get("tbr") or f.get("height") or 0)
                elif info.get("url"):
                    chosen = {"url": info["url"], "protocol": "https"}
                if not chosen or not chosen.get("url"):
                    return False
                video.stream_url = chosen["url"]
                video.stream_kind = "hls" if "m3u8" in (chosen.get("protocol") or "") or ".m3u8" in chosen["url"] else "mp4"
                hdrs = {"User-Agent": USER_AGENT, "Referer": voe_url}
                http_hdrs = chosen.get("http_headers") or {}
                hdrs.update(http_hdrs)
                video.stream_headers = hdrs
                if info.get("title") and not video.title:
                    video.title = info["title"]
                return True
        except Exception as e:
            self.log.debug(f"  [{self.NAME}] yt-dlp VOE resolve: {e}")
            return False


# ── Registry ──────────────────────────────────────────────────────────────

ALL_SCRAPER_CLASSES = [
    # KVS family (most likely to return results for cam performers)
    CamwhoresTV, CamwhoresVideo, CamwhoresCO, CamwhoresHD,
    CamwhoresBay, CamwhoresBayTV, CamwhoresBZ, CamwhoresCloud,
    CamVideosTV, CamhubCC, CamwhCom, CambroTV, CamCapsTV, CamStreamsTV,
    Porntrex,
    # Multi-platform aggregators
    Camsrip,
    # Direct backends
    Recordbate,
    Archivebate,
    # HLS-based
    CamCapsIO,
    # Subscription-platform mirrors (OnlyFans/Fansly/Patreon content, NO auth needed)
    Coomer, Kemono,
    # Mainstream with API
    RedGifs, RedditUser,
    # Auth-required (only activates if cookies_file is set with valid cookies)
    XCom, Recume,
    # Login-required (username/password — auto-login on first probe)
    CamSmut,
]


def load_scrapers(log: logging.Logger, enabled_names: Optional[List[str]] = None,
                   cookies_file: str = "",
                   site_credentials: Optional[Dict[str, Dict[str, str]]] = None,
                   ) -> List[SiteScraper]:
    """Instantiate all custom scrapers.

    cookies_file: optional Netscape cookies.txt path. Each scraper filters
    cookies by its COOKIE_DOMAIN when present.

    site_credentials: optional dict mapping site name -> {"username": ..., "password": ...}
    for scrapers that support username/password login (e.g. camsmut).
    """
    # Apply credentials to class attributes BEFORE instantiation so __init__
    # can pick them up if needed.
    if site_credentials:
        if "camsmut" in site_credentials:
            CamSmut.USERNAME = site_credentials["camsmut"].get("username", "")
            CamSmut.PASSWORD = site_credentials["camsmut"].get("password", "")

    out: List[SiteScraper] = []
    for cls in ALL_SCRAPER_CLASSES:
        if enabled_names and cls.NAME not in enabled_names:
            continue
        try:
            out.append(cls(log, cookies_file=cookies_file))
        except Exception as e:
            log.warning(f"Failed to init {cls.NAME}: {e}")
    return out


def username_variants(username: str) -> List[str]:
    """Generate conservative variant spellings to try if the exact username misses.

    Only generates variants that are UNIQUELY tied to this user. Does NOT
    drop trailing digits (that would turn 'Macy2000' into 'Macy' and match
    every user named Macy). Does NOT generate short stems.
    """
    variants = [username]
    seen = {username.lower()}

    def _add(v: str):
        # Minimum length 6 to avoid generic matches like "Macy" matching many users
        if v and len(v) >= 6 and v.lower() not in seen:
            variants.append(v)
            seen.add(v.lower())

    # missX <-> missyX (e.g. misstrig <-> missytrig)
    # This is a known dialect variation and the stems are still unique
    if re.match(r"^miss[a-z]", username, re.IGNORECASE):
        _add(username[:4] + "y" + username[4:])
    if username.lower().startswith("missy"):
        _add(username[:4] + username[5:])

    # Underscore <-> dash (same user, different slug)
    if "_" in username:
        _add(username.replace("_", "-"))
    if "-" in username:
        _add(username.replace("-", "_"))

    # NOTE: intentionally NOT dropping trailing digits. That generates
    # variants like Macy2000 -> Macy which match many unrelated users.
    return variants


def video_title_matches_user(url_or_slug: str, username: str) -> bool:
    """Check if a video URL/slug/title plausibly belongs to `username`.
    Used to filter out false positives from search-based scrapers."""
    if not url_or_slug or not username:
        return True  # can't judge → allow
    low_slug = url_or_slug.lower().replace("_", "-")
    low_user = username.lower().replace("_", "-")
    # Direct containment
    if low_user in low_slug:
        return True
    # Stem without trailing digits
    stem = re.sub(r"\d+$", "", low_user)
    if len(stem) >= 6 and stem in low_slug:
        return True
    # Common variants
    if low_user.startswith("miss") and not low_user.startswith("missy"):
        if low_user.replace("miss", "missy", 1) in low_slug:
            return True
    if low_user.startswith("missy"):
        if low_user.replace("missy", "miss", 1) in low_slug:
            return True
    return False


__all__ = [
    "VideoRef", "ProbeHit", "SiteScraper",
    "KVSScraper", "Recordbate", "Archivebate", "CamCapsIO", "CamCapsTV",
    "CamwhoresTV", "CamwhoresCO", "CamwhoresHD", "CamwhoresBay",
    "CamVideosTV", "CamhubCC", "CamwhCom", "CambroTV",
    "ALL_SCRAPER_CLASSES", "load_scrapers",
    "USER_AGENT", "parse_kvs_flashvars", "kvs_get_real_url", "mixdrop_build_url",
]
