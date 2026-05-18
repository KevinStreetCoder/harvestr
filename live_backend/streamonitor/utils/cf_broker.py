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
from pathlib import Path
from typing import Iterable, Dict

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
                import tempfile, os
                profile_dir = os.path.join(
                    tempfile.gettempdir(), "harvestr_patchright_cfbroker_profile")
                os.makedirs(profile_dir, exist_ok=True)
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
            
            # Check if fresh and has cookies
            if age < max_age and data.get("cookies"):
                return data
        except (json.JSONDecodeError, ValueError, OSError) as e:
            # Fall through to minting - stale cookies will be refreshed
            pass
    
    # Mint fresh cookies
    return await mint_cookies_for(domain, visit_urls)


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