# streamonitor/utils/cf_broker.py
# Handles cookie minting via Playwright for Cloudflare challenges
#
# Prefer `patchright` over vanilla `playwright` when installed.
# patchright is a Playwright fork whose stealth patches frequently let
# invisible-managed Cloudflare Turnstile auto-pass without a captcha
# service. Install with: pip install patchright && patchright install chromium

import json
import logging
import sys
import time
import asyncio
from pathlib import Path
from typing import Iterable, Dict, Optional

# Lazy auto-installer for patchright. The helper sits next to the
# universal/ project root; reach it via a small sys.path nudge so this
# nested module can import it without an explicit package path.
_LOG = logging.getLogger(__name__)
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parents[3]  # cf_broker.py -> utils -> streamonitor -> live_backend -> universal
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
try:
    from _patchright_setup import ensure_patchright_async  # type: ignore
except ImportError:
    ensure_patchright_async = None  # type: ignore

_USE_PATCHRIGHT = False
async_playwright = None  # type: ignore
PlaywrightTimeout = None  # type: ignore

if ensure_patchright_async is not None and ensure_patchright_async(_LOG):
    try:
        from patchright.async_api import async_playwright, TimeoutError as PlaywrightTimeout  # type: ignore
        _USE_PATCHRIGHT = True
    except ImportError:
        pass

if async_playwright is None:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

COOKIES_DIR = Path("cookies")
COOKIES_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def cookie_file_for(domain: str) -> Path:
    """Get the cookie file path for a domain."""
    safe_domain = domain.replace("/", "_").replace(":", "_")
    return COOKIES_DIR / f"{safe_domain}.json"


def _serialize(cookies):
    """Serialize cookies to JSON-safe format."""
    keep = ("name", "value", "domain", "path", "expires", "secure", "httpOnly", "sameSite")
    return [{k: c.get(k) for k in keep} for c in cookies]


async def mint_cookies_for(
    domain: str,
    visit_urls: Iterable[str],
    timeout_ms: int = 90000,
    settle_ms: int = 4000,
    headless: bool = True
) -> Dict:
    """
    Mint fresh cookies by visiting URLs with a real browser.
    
    Args:
        domain: Domain to save cookies for
        visit_urls: URLs to visit in sequence
        timeout_ms: Navigation timeout in milliseconds
        settle_ms: Time to wait after each page load
        headless: Whether to run browser headless
    
    Returns:
        Dict with 'ts', 'headers', and 'cookies' keys
    """
    try:
        async with async_playwright() as p:
            # patchright stealth only applies on chromium + launch_persistent_context.
            # When available, use that path: it bypasses invisible-managed
            # Cloudflare Turnstile that the original Firefox path can't.
            # Without patchright, fall back to the original Firefox launch,
            # which historically handles classic CF challenges better.
            cleanup_browser = None
            if _USE_PATCHRIGHT:
                # PER-CALL profile dir so concurrent mint_cookies_for()
                # invocations don't clash on Chrome's profile lock.
                # Older approach with a shared dir would silently fail
                # all-but-one launch when called from parallel tasks.
                # Deleted at the end of the request.
                import tempfile, os, shutil, uuid
                profile_dir = os.path.join(
                    tempfile.gettempdir(),
                    f"harvestr_pw_cfbroker_{os.getpid()}_{uuid.uuid4().hex[:8]}",
                )
                os.makedirs(profile_dir, exist_ok=True)
                try:
                    context = await p.chromium.launch_persistent_context(
                        user_data_dir=profile_dir,
                        channel="chrome",
                        headless=headless,
                        user_agent=DEFAULT_UA,
                        viewport={"width": 1280, "height": 800},
                        locale="en-US",
                        java_script_enabled=True,
                        bypass_csp=True,
                        args=["--no-first-run", "--no-default-browser-check"],
                    )
                except Exception as e:
                    # Patchright launch failed for this call — best-effort
                    # clean up the profile and surface to outer handler.
                    try: shutil.rmtree(profile_dir, ignore_errors=True)
                    except Exception: pass
                    raise
                # No separate browser object to close — context owns it.
                browser = None
            else:
                browser = await p.firefox.launch(
                    headless=headless,
                    args=["--disable-blink-features=AutomationControlled"]
                )
                context = await browser.new_context(
                    user_agent=DEFAULT_UA,
                    viewport={"width": 1280, "height": 800},
                    locale="en-US",
                    java_script_enabled=True,
                    bypass_csp=True,
                )

            page = await context.new_page()
            
            # Visit each URL in sequence
            for url in visit_urls:
                try:
                    await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms
                    )
                    # Wait for any CF challenges to resolve
                    await page.wait_for_timeout(settle_ms)
                    
                    # Check if we got through
                    title = await page.title()
                    if "just a moment" in title.lower():
                        # Still on CF challenge, wait more
                        await page.wait_for_timeout(settle_ms * 2)
                
                except PlaywrightTimeout:
                    # Timeout is okay, we might have enough cookies
                    pass
                except Exception as e:
                    # Continue to next URL - broker errors don't need logging
                    continue
            
            # Get all cookies
            cookies = await context.cookies()

            await context.close()
            if browser is not None:
                # Only valid for the Firefox fallback path; the patchright
                # launch_persistent_context owns its own browser, which
                # is closed by context.close() above.
                await browser.close()

        # Clean up the per-call profile dir created above (patchright
        # path only — the Firefox fallback never sets profile_dir).
        if _USE_PATCHRIGHT:
            try:
                import shutil
                if 'profile_dir' in locals():
                    shutil.rmtree(profile_dir, ignore_errors=True)
            except Exception:
                pass

        # Prepare data
        data = {
            "ts": int(time.time()),
            "headers": dict(DEFAULT_HEADERS),
            "cookies": _serialize(cookies),
        }
        
        # Save to disk
        write(domain, data)
        
        return data
    
    except Exception as e:
        # Return minimal valid data - broker errors are handled elsewhere
        return {
            "ts": int(time.time()),
            "headers": dict(DEFAULT_HEADERS),
            "cookies": [],
        }


# ── Process-global mint throttle ──────────────────────────────────────
# Each browser launch (Chrome via patchright / Firefox fallback) costs
# 150-400 MB. Every Bot has its OWN CFSessionManager, so without a global
# throttle, N bots that all need cookies for the SAME domain each launch
# their own browser — 607 Chaturbate bots minting chaturbate.com produced
# up to 25 concurrent Chrome instances per second and exhausted RAM.
#
# These asyncio primitives are loop-bound; every bot shares ONE event loop
# (see cf_session._ensure_loop), so they resolve to a single shared
# instance, created lazily on first use exactly like _GLOBAL_HTTP_SEM.
_MINT_MAX_CONCURRENT = 2          # at most this many browsers alive at once
_MINT_DEDUP_WINDOW = 5 * 60       # reuse a sibling bot's mint if newer than this
_MINT_BROWSER_SEM: Optional[asyncio.Semaphore] = None
_MINT_DOMAIN_LOCKS: Dict[str, asyncio.Lock] = {}
_MINT_LAST_TS: Dict[str, float] = {}


def _mint_browser_sem() -> asyncio.Semaphore:
    global _MINT_BROWSER_SEM
    if _MINT_BROWSER_SEM is None:
        _MINT_BROWSER_SEM = asyncio.Semaphore(_MINT_MAX_CONCURRENT)
    return _MINT_BROWSER_SEM


def _mint_domain_lock(domain: str) -> asyncio.Lock:
    lock = _MINT_DOMAIN_LOCKS.get(domain)
    if lock is None:
        lock = asyncio.Lock()
        _MINT_DOMAIN_LOCKS[domain] = lock
    return lock


async def mint_cookies_dedup(domain: str, visit_urls: Iterable[str], **kwargs) -> Dict:
    """Globally-deduplicated, concurrency-capped cookie mint.

    Collapses the N bots that all need cookies for the same domain into a
    single browser launch: the first bot to win the per-domain lock mints,
    every other bot that was waiting reloads the just-written cookie from
    disk instead of launching its own browser. A small global semaphore
    caps how many domains can be minting browsers at the same instant.
    """
    visit_urls = list(visit_urls)
    lock = _mint_domain_lock(domain)
    async with lock:
        # A sibling bot may have minted for this domain while we waited on
        # the lock — if so, reuse its fresh cookie from disk (no browser).
        # (An empty cookie list counts: it's what a successful mint of a
        # cookie-less host writes. A FAILED mint writes nothing, so its disk
        # ts stays older than `last` and we fall through and retry.)
        last = _MINT_LAST_TS.get(domain, 0.0)
        if time.time() - last < _MINT_DEDUP_WINDOW:
            disk = read(domain)
            if isinstance(disk.get("cookies"), list) and disk.get("ts", 0) >= int(last) - 1:
                return disk
        # We're the elected minter for this domain. Cap total live browsers.
        async with _mint_browser_sem():
            data = await mint_cookies_for(domain, visit_urls, **kwargs)
        _MINT_LAST_TS[domain] = time.time()
        return data


async def load_or_mint(
    domain: str,
    visit_urls: Iterable[str],
    max_age: int = 6 * 3600
) -> Dict:
    """
    Load cookies from disk if fresh enough, otherwise mint new ones.
    
    Args:
        domain: Domain to load/mint cookies for
        visit_urls: URLs to visit if minting is needed
        max_age: Maximum age of cookies in seconds (default 6 hours)
    
    Returns:
        Dict with 'ts', 'headers', and 'cookies' keys
    """
    cf = cookie_file_for(domain)
    
    # Try to load existing cookies
    if cf.exists():
        try:
            data = json.loads(cf.read_text())
            ts = int(data.get("ts", 0))
            age = int(time.time()) - ts
            
            # Fresh is enough -- an EMPTY cookie list included. Only a
            # successful mint writes this file (failures return without
            # writing), and JSON/CDN hosts (StreaMate's manifest server, CB
            # and Bonga edges, ...) legitimately set no cookies. Requiring a
            # non-empty list re-launched a headless browser for every new
            # bot session on those hosts: 52 StreaMate bots = 52 serial
            # browser mints (~50s per first status). A Cloudflare challenge
            # that shows up later still re-mints via the request path.
            if age < max_age and isinstance(data.get("cookies"), list):
                return data
        except (json.JSONDecodeError, ValueError, OSError) as e:
            # Fall through to minting - stale cookies will be refreshed
            pass
    
    # Mint fresh cookies (globally deduped + browser-capped)
    return await mint_cookies_dedup(domain, visit_urls)


def write(domain: str, data: dict):
    """
    Write cookie data to disk.
    
    Args:
        domain: Domain to save cookies for
        data: Cookie data dict to save
    """
    try:
        data = dict(data)
        data["ts"] = int(time.time())
        
        cf = cookie_file_for(domain)
        cf.write_text(json.dumps(data, indent=2))
    except Exception as e:
        # Silently fail - cookies will be re-minted on next request
        pass


def read(domain: str) -> Dict:
    """
    Read cookie data from disk.
    
    Args:
        domain: Domain to read cookies for
    
    Returns:
        Cookie data dict or empty dict if not found
    """
    cf = cookie_file_for(domain)
    
    if not cf.exists():
        return {}
    
    try:
        return json.loads(cf.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def delete(domain: str):
    """
    Delete saved cookies for a domain.
    
    Args:
        domain: Domain to delete cookies for
    """
    cf = cookie_file_for(domain)
    
    try:
        if cf.exists():
            cf.unlink()
    except OSError as e:
        # Ignore deletion errors
        pass


def is_fresh(domain: str, max_age: int = 6 * 3600) -> bool:
    """
    Check if saved cookies are fresh enough.
    
    Args:
        domain: Domain to check
        max_age: Maximum age in seconds
    
    Returns:
        True if cookies exist and are fresh
    """
    data = read(domain)
    
    if not data or not data.get("cookies"):
        return False
    
    ts = int(data.get("ts", 0))
    age = int(time.time()) - ts
    
    return age < max_age