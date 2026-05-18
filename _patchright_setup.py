"""
Lazy auto-installer for `patchright` — the stealth-patched Playwright fork
used by harvestr's browser tier to defeat invisible-managed Cloudflare
Turnstile without a captcha service.

Behaviour:
  * Tries `import patchright.sync_api`. If the import succeeds, this
    module is a no-op and the call is essentially free.
  * If patchright is missing, runs the one-time install:
        pip install --quiet patchright
        patchright install chromium    (~180 MB Chromium download)
  * On install success, returns True so the caller can opt into the
    patchright code path.
  * On any failure (pip blocked, offline, package mirror down), returns
    False so the caller falls back to vanilla `playwright`.
  * The install only runs ONCE per process — subsequent calls hit the
    in-memory _OUTCOME cache instantly.

Why lazy instead of a top-of-file `pip install`?
  * Avoids paying ~180 MB / ~2 min on imports that don't need the
    browser tier (most of the codebase doesn't).
  * Lets the caller decide whether to surface the install progress in
    its own log output (a logger can be passed in).
  * Safe to call from threaded code: install runs under a lock so two
    threads can't race two `pip install` commands at the same time.

Usage:
    from _patchright_setup import ensure_patchright_async, ensure_patchright_sync

    if ensure_patchright_sync(logger=my_log):
        from patchright.sync_api import sync_playwright
    else:
        from playwright.sync_api import sync_playwright
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from typing import Optional

_LOCK = threading.Lock()
# Cached outcome of the install attempt; one of None (not tried),
# True (patchright importable now), False (install failed/skipped).
_OUTCOME: Optional[bool] = None


def _try_import(submodule: str) -> bool:
    """Return True if `patchright.<submodule>` imports cleanly."""
    try:
        __import__(f"patchright.{submodule}")
        return True
    except ImportError:
        return False
    except Exception:
        # Catastrophic failures (e.g. patchright partly-installed but
        # missing chromium) → treat as not importable.
        return False


def _install_patchright(logger: Optional[logging.Logger]) -> bool:
    """Run `pip install patchright` then `patchright install chromium`.
    Returns True if both steps succeed and patchright is importable."""
    py = sys.executable

    # Step 1: pip install. Use --quiet so progress doesn't drown the log,
    # but capture stderr so failures are visible.
    if logger:
        logger.info("patchright not installed; running pip install patchright (one-time setup)")
    try:
        r = subprocess.run(
            [py, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "patchright"],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        if logger:
            logger.warning("patchright pip install timed out after 5 min — falling back to playwright")
        return False
    except Exception as e:
        if logger:
            logger.warning(f"patchright pip install crashed: {type(e).__name__}: {e}")
        return False
    if r.returncode != 0:
        if logger:
            stderr_tail = (r.stderr or "").splitlines()[-3:]
            logger.warning(
                f"patchright pip install failed (rc={r.returncode}): "
                f"{' | '.join(stderr_tail)}")
        return False

    # Step 2: download Chromium for patchright.
    if logger:
        logger.info(
            "Downloading Chromium for patchright (~180 MB, one-time)... "
            "this is what the stealth browser tier runs against.")
    # Two invocation paths: `python -m patchright install chromium`
    # works on most installs; `patchright install chromium` works too
    # if the entry-point is on PATH. We try the module form first
    # because it doesn't depend on PATH state.
    candidates = [
        [py, "-m", "patchright", "install", "chromium"],
        ["patchright", "install", "chromium"],
    ]
    for cmd in candidates:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            if logger:
                logger.warning(
                    "Chromium download timed out after 15 min — patchright is "
                    "installed but the browser bundle is missing; falling back to playwright")
            return False
        except Exception as e:
            if logger:
                logger.warning(f"patchright install chromium crashed: {type(e).__name__}: {e}")
            continue
        if r.returncode == 0:
            break
    else:
        if logger:
            logger.warning("Could not download Chromium for patchright; falling back to playwright")
        return False

    # Step 3: drop any cached failed imports so we can re-import cleanly.
    for mod in list(sys.modules.keys()):
        if mod == "patchright" or mod.startswith("patchright."):
            del sys.modules[mod]
    ok = _try_import("sync_api") and _try_import("async_api")
    if not ok and logger:
        logger.warning(
            "patchright installed but post-install import still failed; "
            "falling back to playwright")
    elif ok and logger:
        logger.info("patchright auto-install complete; stealth browser tier active")
    return ok


def _ensure(logger: Optional[logging.Logger]) -> bool:
    """Core ensure-patchright logic shared by the sync and async wrappers.
    Cached so the install runs once per process at most."""
    global _OUTCOME
    if _OUTCOME is not None:
        return _OUTCOME
    with _LOCK:
        if _OUTCOME is not None:
            return _OUTCOME
        # Fast path: already importable.
        if _try_import("sync_api") and _try_import("async_api"):
            _OUTCOME = True
            return True
        # Slow path: run the install.
        _OUTCOME = _install_patchright(logger)
        return _OUTCOME


def ensure_patchright_sync(logger: Optional[logging.Logger] = None) -> bool:
    """Ensure `patchright.sync_api` is importable. Auto-installs on first
    call if missing. Returns True on success, False if the caller should
    fall back to vanilla playwright."""
    return _ensure(logger)


def ensure_patchright_async(logger: Optional[logging.Logger] = None) -> bool:
    """Same as ensure_patchright_sync but documents intent for callers
    using the async API. Install is shared (one package, two API
    surfaces)."""
    return _ensure(logger)


def patchright_status() -> dict:
    """Diagnostic snapshot, for logging on startup."""
    return {
        "outcome": _OUTCOME,
        "sync_importable": _try_import("sync_api"),
        "async_importable": _try_import("async_api"),
    }
