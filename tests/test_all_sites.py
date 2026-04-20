#!/usr/bin/env python3
"""
Comprehensive site-capability test for Harvestr.

For every site declared in sites.json AND every custom scraper in
custom_scrapers.py, try a probe with one or more candidate usernames.

The test reports per-site:
  PASS          — probe hit, got entries, and (if possible) at least one
                  video title matched the queried username
  NOHIT         — probe ran cleanly but found nothing (expected for many
                  site/user combos)
  CONTAMINATED  — probe returned content that appears unrelated to the
                  user (caught by cross-host redirect guard, 404 guard,
                  or username-match filter). This is the bug class we've
                  been fixing — new cases flagged here.
  BROKEN        — probe raised an exception (network/parser/extractor bug)

Run:
    python tests/test_all_sites.py [--site NAME] [--user NAME] [--quiet]

Exit code is the count of BROKEN + CONTAMINATED sites.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

# Bootstrap: add script dir to path so we can import universal_downloader
SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

# Keep yt-dlp quiet during tests
os.environ.setdefault("YT_DLP_VERBOSE", "")

from universal_downloader import (
    UniversalConfig, SiteRegistry, YtdlpEngine, _is_404_playlist,
    _is_cross_host_redirect,
)
import custom_scrapers

# ──────────────────────────────────────────────────────────────────────────
# Known-real users for different site categories. We try each in order
# until we get a hit — that way we don't falsely report a site as dead
# when the test user just doesn't have content there.

# Well-known YouTube / Twitch / mainstream creators
MAINSTREAM_USERS = [
    "LinusTechTips",   # YouTube, popular
    "Disguised",       # Twitch
    "pewdiepie",       # YouTube
]

# Adult-platform users (from existing config / history)
ADULT_USERS = [
    "misstrig",
    "maidenbancy",
    "blondie_254",
    "Abclong500",
]

# For Reddit / X / etc.
SOCIAL_USERS = [
    "gonewild",        # Reddit known subreddit (not user — for test only)
    "PornhubAria",     # Twitter/X if picked
]


def pick_users_for_site(site_name: str, category: str) -> list[str]:
    """Pick a prioritized list of test usernames for this site."""
    if category == "mainstream":
        return MAINSTREAM_USERS + ADULT_USERS  # fallback
    if category in ("adult", "cam", "archive", "misc"):
        return ADULT_USERS
    return ADULT_USERS + MAINSTREAM_USERS


def make_logger(verbose: bool = False) -> logging.Logger:
    log = logging.getLogger("test")
    log.handlers.clear()
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(h)
    log.setLevel(logging.DEBUG if verbose else logging.WARNING)
    return log


def _slug_match(title: str, user: str) -> bool:
    """Permissive: does the title/URL text mention the user (case-insensitive,
    allowing - _ / . as word separators)?"""
    import re
    if not title or not user:
        return False
    norm = lambda s: re.sub(r"[\W_]+", "", s).lower()
    n_user = norm(user)
    if not n_user:
        return False
    # Strict: whole username present
    if n_user in norm(title):
        return True
    # Stem: first 6+ chars (handles "user_2022_04" style slugs)
    if len(n_user) >= 6 and n_user[:6] in norm(title):
        return True
    return False


# ──────────────────────────────────────────────────────────────────────────
# Test runners

def test_ytdlp_site(site, engine, users: list[str], log: logging.Logger,
                    quiet: bool = False) -> dict:
    """Probe a yt-dlp-backed site. Return result dict."""
    result = {"site": site.name, "backend": "yt-dlp", "status": "NOHIT",
              "user": "", "url": "", "entries": 0, "notes": ""}

    for user in users:
        for pattern in site.patterns[:2]:   # only try top-2 patterns to keep fast
            url = pattern.replace("{u}", user)
            t0 = time.time()
            try:
                info = engine.probe(url)
            except Exception as e:
                if not quiet:
                    print(f"    {site.name}/{user}: exception {type(e).__name__}: {e}", flush=True)
                result.update(status="BROKEN", user=user, url=url,
                              notes=f"{type(e).__name__}: {e}")
                return result
            dt = time.time() - t0
            if info is None:
                continue   # 404 / rejected / empty — try next pattern/user
            entries = info.get("entries") or []
            count = sum(1 for e in entries if e)
            # Guard check: did we get contaminated content?
            title = str(info.get("title", ""))
            if _is_404_playlist(title, url):
                result.update(status="CONTAMINATED", user=user, url=url,
                              entries=count, notes=f"404-page title: {title!r}")
                return result
            cross, why = _is_cross_host_redirect(url, info)
            if cross:
                result.update(status="CONTAMINATED", user=user, url=url,
                              entries=count, notes=f"cross-host: {why}")
                return result
            # Check first few entries for username match.
            # Also check the playlist title itself — for mainstream sites
            # like YouTube, per-video titles are topic-descriptive not
            # user-named, but the playlist is named after the user.
            playlist_text = str(info.get("title") or "") + " " + \
                            str(info.get("uploader") or "") + " " + \
                            str(info.get("channel") or "") + " " + \
                            str(info.get("webpage_url") or url)
            if _slug_match(playlist_text, user):
                matched = 10  # playlist is clearly the right user, trust it
            else:
                matched = 0
                for e in entries[:10]:
                    if not e:
                        continue
                    etxt = (str(e.get("title") or "") + " " +
                            str(e.get("webpage_url") or e.get("url") or "") + " " +
                            str(e.get("uploader") or "") + " " +
                            str(e.get("channel") or ""))
                    if _slug_match(etxt, user):
                        matched += 1
            result.update(status="PASS" if count > 0 else "NOHIT",
                          user=user, url=url, entries=count,
                          notes=f"matched {matched}/{min(10, count)} titles in {dt:.1f}s")
            return result
    return result


def test_custom_scraper(scraper, users: list[str], log: logging.Logger,
                        quiet: bool = False) -> dict:
    """Probe a custom scraper. Return result dict."""
    result = {"site": scraper.NAME, "backend": "custom", "status": "NOHIT",
              "user": "", "url": "", "entries": 0, "notes": ""}

    for user in users:
        try:
            t0 = time.time()
            hits = scraper.probe(user)
        except Exception as e:
            if not quiet:
                print(f"    {scraper.NAME}/{user}: exception {type(e).__name__}: {e}", flush=True)
            result.update(status="BROKEN", user=user,
                          notes=f"{type(e).__name__}: {e}")
            return result
        dt = time.time() - t0
        if not hits:
            continue
        hit = hits[0]
        # Try to enumerate a few videos to check for contamination
        try:
            vids = scraper.enumerate(hit, user, limit=10)
        except Exception as e:
            result.update(status="BROKEN", user=user, url=hit.url,
                          entries=hit.entry_count,
                          notes=f"enumerate raised: {type(e).__name__}: {e}")
            return result
        # For non-authoritative scrapers, apply username filter
        authoritative = getattr(scraper, "AUTHORITATIVE_USER", False)
        if vids and not authoritative:
            kept = sum(1 for v in vids if custom_scrapers.video_title_matches_user(
                v.video_url + " " + (v.title or "") + " " + (v.uploader or ""), user))
            if kept == 0 and len(vids) > 3:
                # All filtered out — likely a search contamination
                result.update(status="CONTAMINATED", user=user, url=hit.url,
                              entries=hit.entry_count,
                              notes=f"all {len(vids)} videos filtered off-topic in {dt:.1f}s")
                return result
            matched = kept
        else:
            matched = len(vids)
        result.update(status="PASS" if matched > 0 else ("NOHIT" if not vids else "CONTAMINATED"),
                      user=user, url=hit.url, entries=hit.entry_count,
                      notes=f"enumerated {len(vids)}, username-matched {matched} in {dt:.1f}s")
        return result
    return result


# ──────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--site", help="Only test this one site name")
    ap.add_argument("--user", help="Use only this username (skips the auto-try list)")
    ap.add_argument("--quiet", action="store_true", help="Suppress per-site verbose output")
    ap.add_argument("--timeout-per-site", type=float, default=45.0)
    ap.add_argument("--json", help="Write results to this JSON file")
    args = ap.parse_args()

    log = make_logger(verbose=False)
    cfg = UniversalConfig(SCRIPT_DIR / "config.json")
    registry = SiteRegistry(SCRIPT_DIR / "sites.json", log)
    engine = YtdlpEngine(cfg, log)

    # Load custom scrapers
    scrapers = custom_scrapers.load_scrapers(log, cookies_file=cfg.cookies_file)

    all_sites = []
    for s in registry.sites.values():
        if args.site and args.site != s.name:
            continue
        all_sites.append(("yt-dlp", s.name, s))
    for sc in scrapers:
        if args.site and args.site != sc.NAME:
            continue
        all_sites.append(("custom", sc.NAME, sc))

    if not all_sites:
        print(f"No site matched --site={args.site!r}. Available:")
        for name in sorted([s.name for s in registry.sites.values()]
                           + [sc.NAME for sc in scrapers]):
            print(f"  {name}")
        return 1

    print(f"Testing {len(all_sites)} sites "
          f"({sum(1 for b,_,_ in all_sites if b=='yt-dlp')} yt-dlp, "
          f"{sum(1 for b,_,_ in all_sites if b=='custom')} custom)\n")

    results = []
    for i, (backend, name, obj) in enumerate(all_sites, 1):
        print(f"[{i:2d}/{len(all_sites)}] {backend:<6} {name:<20} ", end="", flush=True)
        users = [args.user] if args.user else \
                pick_users_for_site(name, getattr(obj, "category", "adult"))
        t0 = time.time()
        if backend == "yt-dlp":
            r = test_ytdlp_site(obj, engine, users, log, quiet=args.quiet)
        else:
            r = test_custom_scraper(obj, users, log, quiet=args.quiet)
        r["duration_s"] = round(time.time() - t0, 1)
        icon = {"PASS": "OK ", "NOHIT": "--  ", "CONTAMINATED": "!! ", "BROKEN": "XX "}[r["status"]]
        print(f"{icon} {r['status']:<13} {r.get('user',''):<15} "
              f"entries={r.get('entries', 0):<4} {r['duration_s']}s "
              f"— {r.get('notes','')[:70]}")
        results.append(r)

    # Summary
    print("\n" + "=" * 80)
    tally = Counter(r["status"] for r in results)
    for status in ("PASS", "NOHIT", "CONTAMINATED", "BROKEN"):
        n = tally.get(status, 0)
        print(f"  {status:<15} {n}")

    bad = [r for r in results if r["status"] in ("CONTAMINATED", "BROKEN")]
    if bad:
        print("\nAttention:")
        for r in bad:
            print(f"  {r['status']:<13} {r['site']:<20} ({r['backend']}) "
                  f"{r.get('user', '')}: {r.get('notes', '')}")

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2))
        print(f"\nResults written to {args.json}")

    return tally.get("BROKEN", 0) + tally.get("CONTAMINATED", 0)


if __name__ == "__main__":
    sys.exit(main())
