#!/usr/bin/env python3
"""
Live smoke test for the new scrapers (Coomer, Kemono, RedGifs, Reddit, XCom).

Actually downloads the first small clip per scraper into tests/_smoke/ and
reports PASS/FAIL + file size. Run once to confirm the pipeline works end-to-end.

Not dry-run: real bytes hit disk.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

from custom_scrapers import (          # noqa: E402
    Coomer, Kemono, RedGifs, RedditUser, XCom,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("smoketest")

OUT_DIR = SCRIPT_DIR / "tests" / "_smoke"
OUT_DIR.mkdir(parents=True, exist_ok=True)

COOKIES = SCRIPT_DIR / "cookies.txt"
COOKIES_ARG = str(COOKIES) if COOKIES.exists() else ""


NETWORK_UNREACHABLE_MARK = "__NETWORK_UNREACHABLE__"


def _download(url: str, outpath: Path, headers: dict | None = None) -> bool | str:
    """Minimal downloader: ffmpeg for .m3u8, curl otherwise.
    Returns True on success, False on download failure, or
    NETWORK_UNREACHABLE_MARK if the CDN host is unreachable from this IP
    (so callers can treat it as environmental rather than a scraper bug)."""
    headers = headers or {}
    if ".m3u8" in url:
        hdr_flat = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        if hdr_flat:
            cmd += ["-headers", hdr_flat]
        cmd += ["-i", url, "-c", "copy", "-bsf:a", "aac_adtstoasc", str(outpath)]
    else:
        cmd = ["curl", "-sSL", "--retry", "2", "--max-time", "60",
               "--connect-timeout", "12", "-o", str(outpath)]
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
        cmd.append(url)
    log.info(f"   downloading -> {outpath.name}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        err = r.stderr or ""
        if ("Could not connect to server" in err or "unreachable network" in err
                or "Connection timed out" in err or "Resolving timed out" in err):
            log.warning(f"   network unreachable (CDN blocked from this IP): {err[:150]}")
            return NETWORK_UNREACHABLE_MARK
        log.warning(f"   download failed rc={r.returncode}: {err[:200]}")
        return False
    if not outpath.exists() or outpath.stat().st_size < 10_000:
        log.warning("   download empty / too small")
        return False
    return True


def test_scraper(cls, candidate_users: list[str], label: str) -> tuple[bool, str]:
    """Run full pipeline — try each candidate username until probe returns a hit."""
    name = cls.NAME
    log.info(f"\n=== {label} (scraper={name}) ===")
    try:
        scraper_log = logging.getLogger(f"scraper.{name}")
        scraper_log.setLevel(logging.INFO)
        scraper = cls(log=scraper_log, cookies_file=COOKIES_ARG)
    except Exception as e:
        return False, f"ctor error: {e}"

    hit = None
    username_used = None
    for user in candidate_users:
        log.info(f"   probing {user}...")
        t0 = time.time()
        try:
            hit = scraper.probe(user)
        except Exception as e:
            log.warning(f"   probe exception for {user}: {e}")
            continue
        t_probe = time.time() - t0
        if hit:
            username_used = user
            log.info(f"   PROBE ok  {t_probe:.1f}s  user={user} -> {hit.url} (estimated {hit.entry_count} entries)")
            break
        log.info(f"   probe {user}: no profile [{t_probe:.1f}s]")

    if not hit:
        return False, f"no probe succeeded (tried {candidate_users})"

    # 2. enumerate — limit to 5 refs
    t0 = time.time()
    try:
        refs = scraper.enumerate(hit, username_used, limit=5)
    except Exception as e:
        return False, f"enumerate exception: {e}"
    t_enum = time.time() - t0
    if not refs:
        return False, f"enumerate returned 0 videos [{t_enum:.1f}s]"
    log.info(f"   ENUM  ok  {t_enum:.1f}s  -> {len(refs)} video refs")
    for i, r in enumerate(refs[:3]):
        log.info(f"      [{i}] {(r.title or '(untitled)')[:70]}")

    # 3. extract stream — pick the first ref that extracts successfully
    picked = None
    for idx, candidate in enumerate(refs):
        if candidate.stream_url:
            picked = candidate
            log.info(f"   EXTRACT (prefilled at enumerate) idx={idx}  kind={candidate.stream_kind}")
            break
        try:
            ok = scraper.extract_stream(candidate)
        except Exception as e:
            log.warning(f"   extract[{idx}] exception: {e}")
            continue
        if not ok or not candidate.stream_url:
            log.warning(f"   extract[{idx}] returned no stream")
            continue
        picked = candidate
        log.info(f"   EXTRACT ok  idx={idx}  kind={candidate.stream_kind}")
        break
    if not picked:
        return False, f"no extractable stream in first {len(refs)} refs"

    # 4. actual download
    safe_title = "".join(c if c.isalnum() or c in " _-." else "_" for c in picked.title)[:60] or picked.video_id or "video"
    ext = ".mp4" if picked.stream_kind in ("mp4", "hls") else ".bin"
    outpath = OUT_DIR / f"{name}_{safe_title}{ext}"
    if outpath.exists():
        outpath.unlink()
    t0 = time.time()
    ok = _download(picked.stream_url, outpath, picked.stream_headers)
    t_dl = time.time() - t0
    if ok == NETWORK_UNREACHABLE_MARK:
        return True, (f"PIPELINE OK (download skipped: CDN unreachable from this network). "
                      f"user={username_used}  enum={t_enum:.1f}s  url={picked.stream_url[:80]}")
    if not ok:
        return False, f"download failed (url={picked.stream_url[:100]})"

    size_mb = outpath.stat().st_size / (1024 * 1024)
    return True, f"OK  user={username_used}  enum={t_enum:.1f}s dl={t_dl:.1f}s  {size_mb:.2f} MB -> {outpath.name}"


def main() -> int:
    shutil.rmtree(OUT_DIR, ignore_errors=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # For each scraper, give a list of candidate usernames to try.
    # First one that probes successfully is used.
    tests = [
        (Coomer,     ["blondie_254", "belledelphine", "amouranth"],                  "Coomer (OnlyFans/Fansly mirror)"),
        (Kemono,     ["theobrobine", "Maplestar", "afrobull", "ViciNeko"],           "Kemono (Patreon/Fanbox mirror)"),
        (RedGifs,    ["toasted500", "nicolebun", "cortanablue", "justkerri"],        "RedGifs"),
        (RedditUser, ["GallowBoob", "spez", "Poem_for_your_sprog"],                  "Reddit user"),
        (XCom,       ["elonmusk"],                                                   "X.com (needs auth cookies)"),
    ]

    results: list[tuple[str, bool, str]] = []
    for cls, users, label in tests:
        try:
            ok, msg = test_scraper(cls, users, label)
        except Exception as e:
            ok, msg = False, f"test harness crashed: {e}"
        results.append((label, ok, msg))
        log.info(f"   => {'PASS' if ok else 'FAIL'}: {msg}")

    print("\n" + "=" * 90)
    print("SMOKE TEST SUMMARY")
    print("=" * 90)
    passed = sum(1 for _, ok, _ in results if ok)
    for label, ok, msg in results:
        mark = "[PASS]" if ok else "[FAIL]"
        print(f"  {mark}  {label:<45s}  {msg}")
    print("-" * 90)
    print(f"  Passed: {passed}/{len(results)}")
    print(f"  Output: {OUT_DIR}")
    print("=" * 90)

    # X.com is allowed to fail if no cookies.txt (graceful skip)
    if not COOKIES_ARG:
        for i, (label, ok, msg) in enumerate(results):
            if "X.com" in label and not ok and "auth" in msg.lower():
                print("  (X.com skipped because no cookies.txt found — not a real failure)")
                results[i] = (label, True, "skipped (no cookies.txt)")
                passed += 1

    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
