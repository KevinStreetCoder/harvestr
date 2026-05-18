#!/usr/bin/env python3
"""
Inline embed-host extractors for Harvestr.

CamSmut (and other sites that iframe third-party players) use rotating
embed hosts: VOE.sx, playmogo/doodstream, mixdrop, filemoon, streamlare,
etc. yt-dlp covers some but not all, and a few actively reject yt-dlp
due to piracy-policy choices or bot-detection.

This module provides a three-tier extractor strategy:

  1. no_browser    — cloudscraper + regex. Fast, no deps. Works for most
                     packed-JS embed hosts (DoodStream, MixDrop, VOE w/
                     static payloads, StreamLare, etc.).

  2. ytdlp_inline  — invoke yt-dlp programmatically with impersonate
                     headers. Handles the majority of well-known hosts.

  3. playwright    — headless Chromium. Last resort for hosts that
                     require full JS execution + Cloudflare challenge
                     solving. Lazy-imported; skipped if Playwright isn't
                     installed.

Entry point:  extract_embed_stream(page_url, log) -> EmbedResult or None

The result contains stream_url, stream_kind (hls/mp4), and any headers
the CDN requires (Referer, User-Agent overrides, etc.).
"""
from __future__ import annotations

import base64
import codecs
import logging
import random
import re
import string
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

try:
    import cloudscraper
    _HAS_CLOUDSCRAPER = True
except ImportError:
    _HAS_CLOUDSCRAPER = False

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

# Playwright is imported lazily — first use pays the cost
_PW_AVAILABLE: Optional[bool] = None

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0.0.0 Safari/537.36")


@dataclass
class EmbedResult:
    """Result of extracting a stream URL from an embed page.

    stream_kind is "hls" for .m3u8 playlists, "mp4" for direct MP4s.
    headers carries any Referer/UA overrides the CDN requires."""
    stream_url: str
    stream_kind: str = "mp4"
    headers: dict = field(default_factory=dict)
    source: str = ""   # which extractor tier produced this (for logging)


# ──────────────────────────────────────────────────────────────────────
# Host detection

def detect_host(url: str) -> str:
    """Return a short tag for the embed host: voe, dood, mixdrop, filemoon,
    streamlare, vidoza, streamtape, generic."""
    host = urlparse(url).hostname or ""
    host = host.lower()
    tags = [
        ("voe", ("voe.sx", "voeunblk", "voeunb", "voe-network", "robertordercharacter",
                 "publicemergencyby", "sensibleadvocacy", "suggestitself",
                 "wisestcitybutterfly", "jilliandescribecompany", "suitablyfestival",
                 "edgeon", "voe-un")),
        ("dood", ("doodstream", "dood.re", "dood.to", "dood.so", "dood.cx",
                  "ds2play", "d0000d", "d000d", "d-s.io", "playmogo", "moga-4")),
        ("mixdrop", ("mixdrop", "m1xdrop", "mxdrop")),
        ("filemoon", ("filemoon", "f-lol", "dhtpre", "frembed")),
        ("streamlare", ("streamlare", "slmaxed")),
        ("vidoza", ("vidoza", "vidozanet")),
        ("streamtape", ("streamtape", "strtpe", "strcloud", "streamta.pe")),
        ("kvs", ("camwhores", "camvideos", "camwh", "cambro", "camstreams")),
    ]
    for tag, needles in tags:
        if any(n in host for n in needles):
            return tag
    return "generic"


# ──────────────────────────────────────────────────────────────────────
# Tier 1: no-browser extractors (fast, no deps)

def _get(url: str, *, session=None, timeout: int = 20) -> Optional[str]:
    """GET a URL via cloudscraper (preferred) or requests, returning body text."""
    if session is None:
        if _HAS_CLOUDSCRAPER:
            session = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows"},
            )
        elif _HAS_REQUESTS:
            session = requests.Session()
            session.headers.update({"User-Agent": USER_AGENT})
        else:
            return None
    try:
        r = session.get(url, timeout=timeout, allow_redirects=True)
        if r.status_code != 200:
            return None
        return r.text
    except Exception:
        return None


def _resolve_redirect(url: str, session=None, timeout: int = 15) -> str:
    """Follow a CDN redirect (e.g. voe.sx → CDN domain) without fetching body."""
    if not _HAS_REQUESTS:
        return url
    try:
        if session is None and _HAS_CLOUDSCRAPER:
            session = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows"},
            )
        elif session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": USER_AGENT})
        r = session.head(url, timeout=timeout, allow_redirects=True)
        return r.url or url
    except Exception:
        return url


def _jsunpack(packed: str) -> str:
    """Unpack eval(function(p,a,c,k,e,d) obfuscated JS into readable text."""
    try:
        match = re.search(
            r"}\('(.*)',\s*(\d+),\s*(\d+),\s*'([^']+)'\.split",
            packed, re.DOTALL,
        )
        if not match:
            return ""
        payload, radix, count, keywords = match.groups()
        radix, count = int(radix), int(count)
        keywords = keywords.split("|")

        def _base_n(num: int, base: int) -> str:
            chars = string.digits + string.ascii_lowercase + string.ascii_uppercase
            if num < base:
                return chars[num]
            return _base_n(num // base, base) + chars[num % base]

        lookup = {}
        for i in range(count):
            key = _base_n(i, radix)
            lookup[key] = keywords[i] if i < len(keywords) and keywords[i] else key

        return re.sub(r'\b(\w+)\b', lambda m: lookup.get(m.group(0), m.group(0)), payload)
    except Exception:
        return ""


def extract_voe_no_browser(url: str, log: Optional[logging.Logger] = None) -> Optional[EmbedResult]:
    """Try to extract an m3u8/mp4 URL from a VOE.sx page without a browser.

    VOE uses multiple obfuscation tricks: base64, reversed base64, ROT13+b64,
    or plaintext URLs in the HTML. We try each in turn."""
    resolved = _resolve_redirect(url)
    html = _get(resolved)
    if not html:
        return None

    # Method 1: Direct m3u8/mp4 URL in page source
    for pattern in [
        r"'hls':\s*'(https?://[^']+\.m3u8[^']*)'",
        r'"hls":\s*"(https?://[^"]+\.m3u8[^"]*)"',
        r"'mp4':\s*'(https?://[^']+\.mp4[^']*)'",
        r'"mp4":\s*"(https?://[^"]+\.mp4[^"]*)"',
        r"file:\s*[\"']?(https?://[^\s\"'<>]+\.m3u8[^\s\"'<>]*)",
        r"src=[\"']?(https?://[^\s\"'<>]+\.m3u8[^\s\"'<>]*)",
    ]:
        m = re.search(pattern, html)
        if m and "test-videos" not in m.group(1):
            return EmbedResult(stream_url=m.group(1), stream_kind="hls",
                               headers={"Referer": resolved, "User-Agent": USER_AGENT},
                               source="voe-plain")

    # Methods 2-4: base64 variants
    for b64_match in re.finditer(r'["\']([A-Za-z0-9+/]{40,}={0,2})["\']', html):
        blob = b64_match.group(1)
        for transform_name, transform in (
            ("b64-direct",    lambda s: s),
            ("b64-reversed",  lambda s: s[::-1]),
            ("rot13",         lambda s: codecs.decode(s, "rot_13")),
        ):
            try:
                payload = transform(blob)
                pad = (-len(payload)) % 4
                if pad:
                    payload += "=" * pad
                decoded = base64.b64decode(payload).decode("utf-8", errors="ignore")
                um = re.search(r'(https?://[^\s"\'<>]+\.(?:m3u8|mp4)[^\s"\'<>]*)', decoded)
                if um and "test-videos" not in um.group(1):
                    return EmbedResult(
                        stream_url=um.group(1),
                        stream_kind="hls" if ".m3u8" in um.group(1) else "mp4",
                        headers={"Referer": resolved, "User-Agent": USER_AGENT},
                        source=f"voe-{transform_name}",
                    )
            except Exception:
                continue

    return None


def extract_doodstream_no_browser(url: str,
                                   log: Optional[logging.Logger] = None
                                   ) -> Optional[EmbedResult]:
    """Extract a direct MP4 URL from a DoodStream / playmogo.com page.

    Flow: fetch the page → find pass_md5 path and cookie index →
    GET /pass_md5/{path} with Referer → concatenate with a random
    10-char token + ?token=idx&expiry=now_ms. No browser needed."""
    if not _HAS_CLOUDSCRAPER:
        return None
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows"},
    )
    try:
        r = scraper.get(url, timeout=20)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    html = r.text
    pass_match = re.search(r"'/pass_md5/([^']+)'", html)
    cookie_match = re.search(r"cookieIndex='([^']+)'", html)
    if not pass_match or not cookie_match:
        return None
    pass_path = pass_match.group(1)
    cookie_idx = cookie_match.group(1)
    dom_match = re.match(r"(https?://[^/]+)", url)
    if not dom_match:
        return None
    domain = dom_match.group(1)
    try:
        r2 = scraper.get(f"{domain}/pass_md5/{pass_path}",
                         headers={"Referer": url}, timeout=15)
    except Exception:
        return None
    if r2.status_code != 200 or not r2.text.startswith("http"):
        return None
    base = r2.text.strip()
    rand = "".join(random.choices(string.ascii_letters + string.digits, k=10))
    stream = f"{base}{rand}?token={cookie_idx}&expiry={int(time.time() * 1000)}"
    return EmbedResult(
        stream_url=stream, stream_kind="mp4",
        headers={"Referer": url, "User-Agent": USER_AGENT},
        source="dood-no-browser",
    )


def extract_mixdrop_no_browser(url: str,
                                log: Optional[logging.Logger] = None
                                ) -> Optional[EmbedResult]:
    """Extract a direct MP4 URL from a MixDrop page. MixDrop eval-packs
    the player JS. We fetch the page, detect the packed block, unpack,
    and pull the MP4 URL from the unpacked source."""
    if not _HAS_REQUESTS:
        return None
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT})
        r = s.get(url, timeout=20, allow_redirects=True, verify=False)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    html = r.text
    # Direct MP4 patterns first
    for pattern in (
        r'wurl\s*=\s*"([^"]+)"',
        r'MDCore\.wurl\s*=\s*"([^"]+)"',
    ):
        m = re.search(pattern, html)
        if m:
            wurl = m.group(1)
            if wurl.startswith("//"):
                wurl = "https:" + wurl
            elif not wurl.startswith("http"):
                wurl = "https://" + wurl
            return EmbedResult(stream_url=wurl, stream_kind="mp4",
                               headers={"Referer": url, "User-Agent": USER_AGENT},
                               source="mixdrop-plain")
    # Packed JS
    packed_match = re.search(r"eval\(function\(p,a,c,k,e,[dr]\)\s*\{.+?\}\([^)]+\)\)", html, re.DOTALL)
    if not packed_match:
        return None
    unpacked = _jsunpack(packed_match.group(0))
    if not unpacked:
        return None
    for pattern in (
        r'wurl\s*=\s*"([^"]+)"',
        r'MDCore\.wurl\s*=\s*"([^"]+)"',
    ):
        m = re.search(pattern, unpacked)
        if m:
            wurl = m.group(1)
            if wurl.startswith("//"):
                wurl = "https:" + wurl
            elif not wurl.startswith("http"):
                wurl = "https://" + wurl
            return EmbedResult(stream_url=wurl, stream_kind="mp4",
                               headers={"Referer": url, "User-Agent": USER_AGENT},
                               source="mixdrop-packed")
    return None


def extract_filemoon_no_browser(url: str,
                                 log: Optional[logging.Logger] = None
                                 ) -> Optional[EmbedResult]:
    """Filemoon / f-lol.com — also eval-packed with m3u8 URL in payload."""
    html = _get(url)
    if not html:
        return None
    packed_match = re.search(r"eval\(function\(p,a,c,k,e,[dr]\)\s*\{.+?\}\([^)]+\)\)",
                             html, re.DOTALL)
    if packed_match:
        unpacked = _jsunpack(packed_match.group(0))
        m = re.search(r"file:\s*[\"']([^\"']+\.m3u8[^\"']*)[\"']", unpacked or "")
        if m:
            return EmbedResult(
                stream_url=m.group(1), stream_kind="hls",
                headers={"Referer": url, "User-Agent": USER_AGENT},
                source="filemoon-packed",
            )
    m = re.search(r"file:\s*[\"']([^\"']+\.m3u8[^\"']*)[\"']", html)
    if m:
        return EmbedResult(
            stream_url=m.group(1), stream_kind="hls",
            headers={"Referer": url, "User-Agent": USER_AGENT},
            source="filemoon-plain",
        )
    return None


# ──────────────────────────────────────────────────────────────────────
# Tier 2: yt-dlp with impersonation

def extract_via_ytdlp(url: str,
                      log: Optional[logging.Logger] = None
                      ) -> Optional[EmbedResult]:
    """Delegate to yt-dlp's built-in extractor with Chrome impersonation
    headers. Handles VOE, many tube-site embeds, StreamTape, Vidoza, etc."""
    try:
        import yt_dlp
    except ImportError:
        return None
    opts = {
        "quiet": True, "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
        "extractor_args": {"generic": {"impersonate": ["chrome131"]}},
        "socket_timeout": 15,
        "retries": 0,
        "extractor_retries": 0,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        if log:
            log.debug(f"  ytdlp embed: {type(e).__name__}: {e}")
        return None
    if not info:
        return None
    fmts = info.get("formats") or []
    hls = [f for f in fmts if (f.get("protocol") or "").startswith("m3u8")]
    mp4 = [f for f in fmts if f.get("ext") == "mp4" and f.get("url")]
    chosen = None
    if hls:
        chosen = max(hls, key=lambda f: f.get("tbr") or f.get("height") or 0)
    elif mp4:
        chosen = max(mp4, key=lambda f: f.get("tbr") or f.get("height") or 0)
    elif info.get("url"):
        chosen = {"url": info["url"]}
    if not chosen or not chosen.get("url"):
        return None
    stream_url = chosen["url"]
    is_hls = ("m3u8" in (chosen.get("protocol") or "") or ".m3u8" in stream_url)
    hdrs = {"User-Agent": USER_AGENT, "Referer": url}
    hdrs.update(chosen.get("http_headers") or {})
    return EmbedResult(stream_url=stream_url,
                       stream_kind="hls" if is_hls else "mp4",
                       headers=hdrs, source="ytdlp")


# ──────────────────────────────────────────────────────────────────────
# Tier 3: Playwright (headless Chromium)

_PW_CTX = None   # module-level browser context, reused across calls
_PW_PLAYWRIGHT = None


# Comprehensive stealth init script — hides automation tells beyond
# `navigator.webdriver`. Cloudflare Turnstile fingerprints navigator.*,
# WebGL, plugins, and chrome.runtime; our previous one-liner only
# covered .webdriver, which means Turnstile was still flagging the
# session as automation. This expanded script covers all the common
# detection vectors.
_STEALTH_INIT_SCRIPT = r"""
(() => {
    'use strict';
    try { Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => undefined, configurable: true }); } catch (e) {}
    try {
        const fakePlugins = [
            { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        ];
        Object.defineProperty(Navigator.prototype, 'plugins', {
            get: () => Object.assign(fakePlugins, { length: fakePlugins.length, item: i => fakePlugins[i], namedItem: n => fakePlugins.find(p => p.name === n) || null, refresh: () => {} }),
            configurable: true,
        });
    } catch (e) {}
    try { Object.defineProperty(Navigator.prototype, 'languages', { get: () => ['en-US', 'en'], configurable: true }); } catch (e) {}
    try {
        if (!window.chrome) window.chrome = {};
        if (!window.chrome.runtime) window.chrome.runtime = {};
        if (!window.chrome.app) window.chrome.app = { isInstalled: false };
    } catch (e) {}
    try {
        const origQuery = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = (params) => (
            params && params.name === 'notifications'
                ? Promise.resolve({ state: 'default', onchange: null })
                : origQuery(params)
        );
    } catch (e) {}
    try {
        const orig = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function (p) {
            if (p === 37445) return 'Intel Inc.';
            if (p === 37446) return 'Intel Iris OpenGL Engine';
            return orig.call(this, p);
        };
    } catch (e) {}
})();
"""


def _ensure_playwright(log: Optional[logging.Logger] = None):
    """Lazy-init a headless-Chromium context. Returns (playwright, context)
    or (None, None) if Playwright isn't installed / fails to launch.

    Prefer `patchright` over vanilla `playwright` when installed.
    patchright is a community Playwright fork whose stealth patches
    frequently let invisible-managed Cloudflare Turnstile auto-pass
    without any captcha service. Install:
      pip install patchright && patchright install chromium

    Stealth applies only when patchright LAUNCHES the browser itself
    (not when attaching over CDP), and only when it uses
    `launch_persistent_context` — so the patchright branch below
    routes through there. The init-script-based stealth we apply for
    vanilla Playwright is deliberately SKIPPED on the patchright
    branch (layering on top breaks navigator.* enough to produce
    net::ERR_NAME_NOT_RESOLVED on subsequent goto calls)."""
    global _PW_CTX, _PW_PLAYWRIGHT, _PW_AVAILABLE
    if _PW_AVAILABLE is False:
        return None, None
    if _PW_CTX is not None:
        return _PW_PLAYWRIGHT, _PW_CTX
    sync_playwright = None
    use_patchright = False
    # Try patchright first. If it isn't installed yet, attempt the
    # one-time auto-install (pip install patchright + patchright install
    # chromium). The helper caches the outcome so this is a no-op on
    # subsequent calls in the same process.
    try:
        from _patchright_setup import ensure_patchright_sync  # type: ignore
    except ImportError:
        ensure_patchright_sync = None  # type: ignore
    if ensure_patchright_sync is not None and ensure_patchright_sync(log):
        try:
            from patchright.sync_api import sync_playwright as _sp  # type: ignore
            sync_playwright = _sp
            use_patchright = True
            if log:
                log.debug("  Using patchright (stealth) for browser tier")
        except ImportError:
            pass
    if sync_playwright is None:
        try:
            from playwright.sync_api import sync_playwright as _sp
            sync_playwright = _sp
        except ImportError:
            _PW_AVAILABLE = False
            if log:
                log.debug("  Playwright not installed — browser tier disabled")
            return None, None
    try:
        pw = sync_playwright().start()
        if use_patchright:
            # patchright stealth requires launch_persistent_context.
            # Use a dedicated profile dir so we don't clash with any
            # legacy playwright profile state on the same machine.
            import tempfile, os
            profile_dir = os.path.join(
                tempfile.gettempdir(), "harvestr_patchright_profile")
            os.makedirs(profile_dir, exist_ok=True)
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                channel="chrome",
                headless=True,
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                timezone_id="America/New_York",
                no_viewport=False,
                args=["--no-first-run", "--no-default-browser-check"],
            )
            # DELIBERATELY skip _STEALTH_INIT_SCRIPT: patchright already
            # patches the same surface, and layering ours on top has
            # been observed to break navigator.* in Chrome 147+
            # (subsequent goto raises net::ERR_NAME_NOT_RESOLVED).
        else:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    # Reduce detection surface: Cloudflare's bot-score uses
                    # subresource integrity + automation-flag checks; these
                    # args turn off the most-fingerprintable defaults.
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--disable-site-isolation-trials",
                ],
            )
            ctx = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                # Mimic a real Chrome timezone (most automation tools leak UTC)
                timezone_id="America/New_York",
            )
            # Comprehensive stealth — replaces the previous 1-line webdriver
            # hide with a full fingerprint mask covering plugins, languages,
            # chrome.runtime, permissions, and WebGL vendor.
            ctx.add_init_script(_STEALTH_INIT_SCRIPT)
        _PW_PLAYWRIGHT = pw
        _PW_CTX = ctx
        _PW_AVAILABLE = True
        return pw, ctx
    except Exception as e:
        _PW_AVAILABLE = False
        if log:
            log.warning(f"  Playwright launch failed: {e}")
        return None, None


def _click_turnstile_real_mouse(page, log: Optional[logging.Logger] = None) -> bool:
    """Click the Cloudflare Turnstile checkbox using REAL Playwright mouse
    events (not synthetic JS). Cloudflare's Turnstile widget specifically
    checks `event.isTrusted` — synthetic JS clicks (`element.click()`,
    `dispatchEvent(new MouseEvent('click'))`) all have isTrusted=false
    and are rejected. Playwright's page.mouse.click() generates a trusted
    event via CDP that Turnstile accepts.

    Tries multiple selectors because Turnstile renders differently
    depending on the challenge mode and lazy-loading state:
      - iframe[src*=challenges.cloudflare.com]: classic interactive
      - iframe[data-src*=...]: pre-bootstrap (src not set yet)
      - div.cf-turnstile: outer container (always present once the
        Turnstile script has initialized)
      - div[id^=cf-chl-widget]: managed challenge container
      - sole iframe on page: most embed pages have only the CF iframe
        before the player loads.
    Returns True if a click was issued. False if no widget was visible
    — caller should retry on a later poll."""
    import random
    try:
        selectors = (
            'iframe[src*="challenges.cloudflare.com"]',
            'iframe[data-src*="challenges.cloudflare.com"]',
            'iframe[src*="turnstile"]',
            'iframe[title*="Cloudflare"]',
            'iframe[title*="security challenge"]',
            'div.cf-turnstile',
            'div[id^="cf-chl-widget"]',
        )
        target_loc = None
        chosen_sel = ""
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    continue
                try:
                    loc.wait_for(state="visible", timeout=2000)
                except Exception:
                    pass
                bb = loc.bounding_box()
                if bb and bb.get("width", 0) >= 50 and bb.get("height", 0) >= 30:
                    target_loc = loc
                    chosen_sel = sel
                    break
            except Exception:
                continue
        if target_loc is None:
            try:
                all_iframes = page.locator('iframe')
                if all_iframes.count() == 1:
                    only = all_iframes.first
                    bb = only.bounding_box()
                    if bb and bb.get("width", 0) >= 50:
                        target_loc = only
                        chosen_sel = 'iframe (sole on page)'
            except Exception:
                pass
            if target_loc is None:
                if log:
                    log.debug("  Turnstile: no iframe match (lazy-loaded?)")
                return False
        box = target_loc.bounding_box()
        if not box:
            return False
        target_x = box["x"] + 30
        target_y = box["y"] + box["height"] / 2
        page.mouse.move(target_x - 200, target_y - 80, steps=8)
        page.mouse.move(target_x - 50, target_y - 20, steps=10)
        page.mouse.move(target_x, target_y, steps=8)
        page.wait_for_timeout(300 + random.randint(120, 400))
        page.mouse.click(target_x, target_y, delay=random.randint(40, 110))
        if log:
            log.info(f"  Turnstile: clicked {chosen_sel} at "
                       f"({target_x:.0f},{target_y:.0f}) "
                       f"box={box['width']:.0f}x{box['height']:.0f}")
        return True
    except Exception as e:
        if log:
            log.debug(f"  Turnstile click failed: {type(e).__name__}: {e}")
        return False


def shutdown_playwright() -> None:
    """Cleanly stop the shared browser context. Call at session end."""
    global _PW_CTX, _PW_PLAYWRIGHT
    if _PW_CTX is not None:
        try:
            _PW_CTX.close()
        except Exception:
            pass
        _PW_CTX = None
    if _PW_PLAYWRIGHT is not None:
        try:
            _PW_PLAYWRIGHT.stop()
        except Exception:
            pass
        _PW_PLAYWRIGHT = None


def _pw_wait_cf(page, timeout: int = 45) -> bool:
    """Wait through a Cloudflare challenge page. Returns True when
    the challenge is past (title doesn't mention Cloudflare/just a moment)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            title = (page.title() or "").lower()
            if "just a moment" not in title and "cloudflare" not in title and \
               "verifying" not in title:
                return True
        except Exception:
            pass
        page.wait_for_timeout(1000)
    return False


def extract_via_playwright(url: str,
                            log: Optional[logging.Logger] = None
                            ) -> Optional[EmbedResult]:
    """Last-resort extractor: load the embed page in headless Chrome,
    intercept .m3u8/.mp4 network requests, and probe JWPlayer's JS API."""
    pw, ctx = _ensure_playwright(log)
    if ctx is None:
        return None
    resolved = _resolve_redirect(url) or url
    page = ctx.new_page()
    intercepted: dict = {}

    def on_req(req):
        ru = req.url
        if ".m3u8" in ru and "test-videos" not in ru:
            intercepted.setdefault("stream", ru)
        elif ".mp4" in ru and "test-videos" not in ru and "logo" not in ru:
            intercepted.setdefault("stream", ru)

    try:
        page.on("request", on_req)
        page.goto(resolved, wait_until="domcontentloaded", timeout=30000)
        _pw_wait_cf(page, timeout=45)
        # If a Cloudflare Turnstile widget is rendered, click it via real
        # mouse events. Wait 8s first to give "auto" mode a chance to
        # self-resolve; then click and wait. Re-attempt every 12s up to
        # ~50s total (matches our standalone timing budget).
        try:
            page.wait_for_timeout(8000)
            for _ in range(4):
                if intercepted.get("stream"):
                    break
                turnstile_present = bool(page.locator(
                    'iframe[src*="challenges.cloudflare.com"]'
                ).count())
                if not turnstile_present:
                    break
                clicked = _click_turnstile_real_mouse(page, log=log)
                page.wait_for_timeout(12000 if clicked else 4000)
        except Exception as e:
            if log:
                log.debug(f"  Turnstile bypass attempt: {type(e).__name__}: {e}")
        page.wait_for_timeout(5000)

        stream = intercepted.get("stream")
        if not stream:
            # Trigger play via JWPlayer / video element
            try:
                page.evaluate("""() => {
                    for (const sel of ['.jw-icon-display', '.jw-video', 'video',
                                        '.player-wrapper']) {
                        const el = document.querySelector(sel);
                        if (el) { el.click(); break; }
                    }
                    if (typeof jwplayer !== 'undefined') {
                        try { jwplayer().play(); } catch(_) {}
                    }
                }""")
                page.wait_for_timeout(5000)
                stream = intercepted.get("stream")
            except Exception:
                pass

        if not stream:
            # JWPlayer API poll
            try:
                val = page.evaluate("""() => {
                    if (typeof jwplayer !== 'undefined') {
                        try {
                            const item = jwplayer().getPlaylistItem();
                            return item.file || item.src || '';
                        } catch(_) { return ''; }
                    }
                    return '';
                }""")
                if val and ("m3u8" in val or ".mp4" in val) and \
                        "test-videos" not in val:
                    stream = val
            except Exception:
                pass

        if not stream:
            # Regex on final DOM
            try:
                content = page.content()
                for pattern in (
                    r"'hls':\s*'(https?://[^']+\.m3u8[^']*)'",
                    r'"hls":\s*"(https?://[^"]+\.m3u8[^"]*)"',
                    r"file:\s*[\"']?(https?://[^\s\"'<>]+\.m3u8[^\s\"'<>]*)",
                    r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)',
                ):
                    m = re.search(pattern, content)
                    if m and "test-videos" not in m.group(1) and \
                            "logo" not in m.group(1):
                        stream = m.group(1)
                        break
            except Exception:
                pass

        if not stream:
            return None
        is_hls = ".m3u8" in stream
        return EmbedResult(stream_url=stream,
                           stream_kind="hls" if is_hls else "mp4",
                           headers={"Referer": resolved, "User-Agent": USER_AGENT},
                           source="playwright")
    except Exception as e:
        if log:
            log.debug(f"  Playwright extract error: {e}")
        return None
    finally:
        try:
            page.close()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────
# Top-level entry point

def extract_embed_stream(url: str,
                          log: Optional[logging.Logger] = None,
                          allow_browser: bool = True,
                          ) -> Optional[EmbedResult]:
    """Extract the playable stream URL from an embed-host page.

    Tries (in order):
      1. Host-specific no-browser extractor (fast, most reliable)
      2. yt-dlp inline (handles many generic hosts)
      3. Playwright headless Chromium (fallback for JS-heavy hosts)

    Returns None if every tier failed — caller should mark the video
    skip (not permanent-fail) so a future yt-dlp / site fix can retry."""
    if not url:
        return None
    host = detect_host(url)

    no_browser_fn = {
        "voe": extract_voe_no_browser,
        "dood": extract_doodstream_no_browser,
        "mixdrop": extract_mixdrop_no_browser,
        "filemoon": extract_filemoon_no_browser,
    }.get(host)

    if no_browser_fn:
        res = no_browser_fn(url, log)
        if res:
            if log:
                log.debug(f"  embed: {host} via {res.source}")
            return res

    # Tier 2: yt-dlp
    res = extract_via_ytdlp(url, log)
    if res:
        if log:
            log.debug(f"  embed: {host} via {res.source}")
        return res

    # Tier 3: Playwright
    if allow_browser:
        res = extract_via_playwright(url, log)
        if res:
            if log:
                log.info(f"  embed: {host} via {res.source} (browser fallback)")
            return res

    return None
