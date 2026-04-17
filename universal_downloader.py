#!/usr/bin/env python3
"""
Universal Video Downloader — yt-dlp powered
============================================
Given a performer/streamer username, probe all configured video sites to find
their profile/channel pages, enumerate videos, and download them.

Architecture
------------
  [username] -> [parallel probe sites] -> [rank hits by video count]
             -> [enumerate video URLs per site] -> [filter via history]
             -> [download via yt-dlp + aria2c external downloader]
             -> [persist history atomically]

Design principles
-----------------
  - Leverage yt-dlp's 1800+ built-in extractors. No per-site anti-bot code here.
  - Thread-safe, atomic JSON state (history, failed, combo counts).
  - Rolling-window: re-runs pick up the next batch of new videos per performer.
  - aria2c multi-segment downloads where the site supports Range requests.
  - Graceful degradation: per-site failure doesn't block other sites.
  - Fully configurable: enabled sites, per-site URL patterns, rate limits.

Usage
-----
  python universal_downloader.py <username>                 # specific performer
  python universal_downloader.py --all                      # every performer in config
  python universal_downloader.py <username> --sites pornhub,xvideos
  python universal_downloader.py --list-sites               # show supported sites
  python universal_downloader.py --save-config              # write template config
  python universal_downloader.py --dry-run <username>       # probe only, no downloads
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import re
import shutil
import string
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any, Iterable

if sys.platform == "win32":
    import io
    if not isinstance(sys.stdout, io.TextIOWrapper) or sys.stdout.encoding != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if not isinstance(sys.stderr, io.TextIOWrapper) or sys.stderr.encoding != "utf-8":
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

try:
    import yt_dlp
except ImportError:
    print("ERROR: yt-dlp is not installed. Run: pip install -U \"yt-dlp[default,curl-cffi]\"")
    sys.exit(1)

# Custom scrapers for cam archive sites not supported by yt-dlp
import custom_scrapers
from custom_scrapers import (
    load_scrapers as _load_custom_scrapers,
    username_variants as _username_variants,
    SiteScraper as _CustomSiteScraper,
)

# Shared live-progress tracker (writes downloads/_progress.json for the UI).
from progress_tracker import ProgressTracker, make_yt_dlp_hook

try:
    from rich.console import Console
    from rich.progress import (
        Progress, SpinnerColumn, TextColumn, BarColumn,
        TimeElapsedColumn, TaskProgressColumn,
    )
    from rich.table import Table
    from rich.panel import Panel
    from rich.logging import RichHandler
    HAVE_RICH = True
except ImportError:
    HAVE_RICH = False

SCRIPT_DIR = Path(__file__).resolve().parent
console = Console() if HAVE_RICH else None

# ── aria2c auto-detection ─────────────────────────────────────────────────────
ARIA2C_PATH = ""
for _candidate in [
    r"C:\Users\Street Coder\AppData\Local\Microsoft\WinGet\Packages\aria2.aria2_Microsoft.Winget.Source_8wekyb3d8bbwe\aria2-1.37.0-win-64bit-build1\aria2c.exe",
    r"C:\ProgramData\chocolatey\bin\aria2c.exe",
    shutil.which("aria2c") or "aria2c",
]:
    try:
        result = subprocess.run([_candidate, "--version"], capture_output=True, timeout=5)
        if result.returncode == 0:
            ARIA2C_PATH = _candidate
            break
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        continue

# ── ffmpeg auto-detection ─────────────────────────────────────────────────────
FFMPEG_PATH = ""
for _candidate in [
    r"C:\ffmpeg\bin\ffmpeg.exe",
    shutil.which("ffmpeg") or "ffmpeg",
]:
    try:
        result = subprocess.run([_candidate, "-version"], capture_output=True, timeout=5)
        if result.returncode == 0:
            FFMPEG_PATH = _candidate
            break
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        continue

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


# ── Logging ───────────────────────────────────────────────────────────────────
def setup_logging(log_dir: Path, verbose: bool = False) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "universal.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    if HAVE_RICH:
        console_handler = RichHandler(rich_tracebacks=True, show_path=False, console=console)
        console_handler.setFormatter(logging.Formatter("%(message)s"))
    else:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)

    file_handler = logging.FileHandler(str(log_file), encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    root.handlers.clear()
    root.addHandler(console_handler)
    root.addHandler(file_handler)

    # Silence noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("yt_dlp").setLevel(logging.WARNING)

    return logging.getLogger("universal")


# ── Data Models ───────────────────────────────────────────────────────────────
@dataclass
class SiteConfig:
    name: str
    category: str = "misc"
    patterns: List[str] = field(default_factory=list)
    yt_dlp_extractor: str = ""
    supports_flat: bool = True
    notes: str = ""


@dataclass
class ProbeHit:
    """A successful probe: this site has videos for this username at this URL."""
    site: str
    url: str
    entry_count: int = 0  # from flat extraction
    uploader_id: str = ""


@dataclass
class VideoRef:
    """Reference to a single video discovered on a site."""
    site: str
    video_id: str            # site-native id (yt-dlp's `id` field)
    video_url: str           # canonical URL for yt-dlp to download
    title: str = ""
    uploader: str = ""
    uploader_id: str = ""
    duration: float = 0.0
    performer: str = ""       # the query username we used to find this
    # Populated by custom scrapers (yt-dlp fills these at download time instead)
    stream_url: str = ""
    stream_kind: str = ""    # "mp4" | "hls" | ""
    stream_headers: Dict[str, str] = field(default_factory=dict)
    is_custom: bool = False   # True if this came from a custom scraper (not yt-dlp)

    @property
    def global_id(self) -> str:
        """Globally unique ID for dedup: site|video_id."""
        return f"{self.site}|{self.video_id}"


@dataclass
class UniversalConfig:
    output_dir: str = str(SCRIPT_DIR / "downloads")
    performers: List[str] = field(default_factory=list)
    enabled_sites: List[str] = field(default_factory=list)   # empty = all sites from sites.json
    max_videos_per_site: int = 10
    min_probe_entries: int = 2     # reject probe hits with fewer than N videos (1-video hits are usually placeholders)
    max_parallel_probes: int = 8
    max_parallel_downloads: int = 3
    min_disk_gb: float = 5.0
    use_aria2c: bool = True
    aria2c_connections: int = 16
    rate_limit: str = ""                                      # e.g. "500K" for 500KB/s per download
    cookies_from_browser: str = ""                            # e.g. "chrome", "firefox"
    cookies_file: str = ""                                    # Netscape cookies.txt path
    impersonate_target: str = "chrome"                         # curl_cffi impersonation
    min_duration_seconds: float = 30.0                         # skip very short clips
    retries: int = 5
    probe_timeout: int = 30
    verbose: bool = False
    # Site-specific credentials (login for camsmut etc.)
    camsmut_username: str = ""
    camsmut_password: str = ""

    @classmethod
    def load(cls, path: Path) -> "UniversalConfig":
        if not path.exists():
            return cls()
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        valid = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**valid)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)


# ── Atomic JSON store (reusable for history, failed, etc.) ────────────────────
class AtomicJsonStore:
    """Thread-safe JSON store with atomic writes. Handles Windows file-lock
    races by retrying os.replace() and using per-thread temp file names."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                # Merge with any concurrent disk changes to avoid overwriting them
                if self.path.exists():
                    try:
                        with open(self.path, encoding="utf-8") as f:
                            disk = json.load(f)
                        self._merge_disk(disk)
                    except (json.JSONDecodeError, OSError):
                        pass
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, indent=2, ensure_ascii=False, sort_keys=True)
                for attempt in range(6):
                    try:
                        os.replace(tmp, self.path)
                        break
                    except PermissionError:
                        if attempt == 5:
                            raise
                        time.sleep(0.1 * (attempt + 1))
            except Exception:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                raise

    def _merge_disk(self, disk: dict) -> None:
        """Merge disk state into self.data. Override for domain-specific merge."""
        for k, v in disk.items():
            if k not in self.data:
                self.data[k] = v


class DownloadHistory(AtomicJsonStore):
    """Tracks downloaded videos, keyed by (performer, global_id).

    Schema:
      {
        "performer_lower": {
          "site|video_id": {
            "title": "...", "url": "...", "output": "...",
            "date": "...", "filesize": N, "duration": N, ...
          }
        }
      }
    """

    def _merge_disk(self, disk: dict) -> None:
        # Nested merge: performer -> global_id
        for perf, entries in disk.items():
            if perf not in self.data:
                self.data[perf] = entries
            else:
                for gid, info in entries.items():
                    if gid not in self.data[perf]:
                        self.data[perf][gid] = info

    def is_downloaded(self, performer: str, global_id: str) -> bool:
        return global_id in self.data.get(performer.lower(), {})

    def mark_downloaded(self, video: VideoRef, output_path: str = "", filesize: int = 0) -> None:
        with self._lock:
            key = video.performer.lower()
            if key not in self.data:
                self.data[key] = {}
            self.data[key][video.global_id] = {
                "site": video.site,
                "video_id": video.video_id,
                "url": video.video_url,
                "title": video.title,
                "output": output_path,
                "filesize": filesize,
                "duration": video.duration,
                "date": datetime.now().isoformat(timespec="seconds"),
            }
        self.save()

    def count(self, performer: str = "") -> int:
        if performer:
            return len(self.data.get(performer.lower(), {}))
        return sum(len(v) for v in self.data.values())


class FailedHistory(AtomicJsonStore):
    """Tracks failed downloads. Permanent flag only for confirmed dead links."""

    MAX_FAILURES = 3

    def _merge_disk(self, disk: dict) -> None:
        for k, v in disk.items():
            if k not in self.data:
                self.data[k] = v

    def is_permanently_failed(self, global_id: str) -> bool:
        e = self.data.get(global_id)
        return e is not None and e.get("permanent", False)

    def record_failure(self, video: VideoRef, reason: str, file_size: int = 0) -> bool:
        with self._lock:
            entry = self.data.get(video.global_id, {"fail_count": 0, "sizes": []})
            entry["fail_count"] = entry.get("fail_count", 0) + 1
            entry["reason"] = reason
            entry["date"] = datetime.now().isoformat(timespec="seconds")
            entry["site"] = video.site
            entry["url"] = video.video_url

            sizes = entry.get("sizes", [])
            if file_size > 0:
                sizes.append(file_size)
            entry["sizes"] = sizes[-5:]

            if entry["fail_count"] >= self.MAX_FAILURES:
                rlow = reason.lower()
                if ("404" in rlow or "dead" in rlow or "not found" in rlow
                        or "deleted" in rlow or "private" in rlow
                        or "members-only" in rlow):
                    entry["permanent"] = True
                elif file_size > 0 and len(sizes) >= 2 and all(abs(s - sizes[0]) < 100_000 for s in sizes):
                    entry["permanent"] = True
            self.data[video.global_id] = entry
        self.save()
        return entry.get("permanent", False)


# ── Site registry ─────────────────────────────────────────────────────────────
class SiteRegistry:
    """Loads the list of supported sites and their URL patterns from sites.json."""

    def __init__(self, sites_path: Path, log: logging.Logger):
        self.log = log
        self.sites: Dict[str, SiteConfig] = {}
        self._load(sites_path)

    def _load(self, path: Path) -> None:
        if not path.exists():
            self.log.error(f"sites.json not found at {path}")
            return
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        skipped = []
        for name, info in data.get("sites", {}).items():
            # Names starting with underscore are disabled
            if name.startswith("_"):
                skipped.append(name)
                continue
            self.sites[name] = SiteConfig(
                name=name,
                category=info.get("category", "misc"),
                patterns=info.get("patterns", []),
                yt_dlp_extractor=info.get("yt_dlp_extractor", ""),
                supports_flat=info.get("supports_flat", True),
                notes=info.get("notes", ""),
            )
        msg = f"Loaded {len(self.sites)} site definitions"
        if skipped:
            msg += f" ({len(skipped)} disabled: {', '.join(s.lstrip('_') for s in skipped)})"
        self.log.info(msg)

    def enabled(self, names: List[str]) -> List[SiteConfig]:
        if not names:
            return list(self.sites.values())
        return [self.sites[n] for n in names if n in self.sites]

    def by_category(self, category: str) -> List[SiteConfig]:
        return [s for s in self.sites.values() if s.category == category]


# ── yt-dlp wrapper ────────────────────────────────────────────────────────────
class _QuietYtdlpLogger:
    """yt-dlp logger adapter. Routes yt-dlp's own messages to our file log
    only (suppressed on console) so per-extractor 404s and 'Unsupported URL'
    don't flood the terminal while we're probing dozens of sites."""

    def __init__(self, target: logging.Logger):
        self._log = target

    def debug(self, msg):
        # yt-dlp uses debug() for both real debug and verbose stdout; filter noise
        if msg.startswith("[debug] "):
            self._log.debug(msg)
        else:
            self._log.debug(msg)

    def info(self, msg):
        self._log.debug(msg)  # demote info to debug

    def warning(self, msg):
        self._log.debug(msg)  # also demote: most yt-dlp warnings are per-URL noise

    def error(self, msg):
        self._log.debug(msg)  # extractor errors on bad URLs; keep in file only


class YtdlpEngine:
    """Thin wrapper around yt-dlp's Python API. Builds YoutubeDL instances with
    consistent options for probing, enumeration, and downloading."""

    def __init__(self, config: UniversalConfig, log: logging.Logger):
        self.config = config
        self.log = log
        self._ytdlp_logger = _QuietYtdlpLogger(logging.getLogger("yt_dlp_silent"))

    def _common_opts(self) -> dict:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "verbose": False,
            "ignoreerrors": True,
            "noplaylist": False,
            "retries": self.config.retries,
            "fragment_retries": self.config.retries,
            "file_access_retries": 3,
            "extractor_retries": self.config.retries,
            "socket_timeout": self.config.probe_timeout,
            "http_headers": {"User-Agent": USER_AGENT},
            "sleep_interval_requests": 1,
            "sleep_interval": 1,
            "max_sleep_interval": 3,
            "logger": self._ytdlp_logger,
            "consoletitle": False,
        }
        if self.config.cookies_from_browser:
            opts["cookiesfrombrowser"] = (self.config.cookies_from_browser,)
        if self.config.cookies_file:
            opts["cookiefile"] = self.config.cookies_file
        # curl_cffi impersonation if available (helps with Cloudflare)
        if self.config.impersonate_target:
            try:
                from yt_dlp.networking.impersonate import ImpersonateTarget
                opts["impersonate"] = ImpersonateTarget(self.config.impersonate_target)
            except (ImportError, AttributeError):
                pass
        return opts

    def probe(self, url: str) -> Optional[dict]:
        """Fast probe: returns playlist metadata with flat entries, or None.
        Uses aggressively-tight retry/timeout settings — probes should fail fast
        on non-existent users. Many probes fail with 404 / Unsupported URL;
        these are expected and routed to the debug log, not console."""
        opts = self._common_opts()
        opts.update({
            "extract_flat": "in_playlist",
            "skip_download": True,
            # Low cap — we just need existence, not an accurate count.
            # Some extractors (e.g. YouPorn) enumerate the whole site when the
            # user doesn't exist. Keep this tight.
            "playlistend": 25,
            "lazy_playlist": True,
            # Probe-specific: fail fast. Each URL should take <= 5s on 404.
            "retries": 0,
            "extractor_retries": 0,
            "fragment_retries": 0,
            "socket_timeout": 10,
            "sleep_interval_requests": 0,
            "sleep_interval": 0,
            "max_sleep_interval": 0,
        })
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    return None
                return ydl.sanitize_info(info)
        except yt_dlp.utils.DownloadError as e:
            self.log.debug(f"Probe failed for {url}: {e}")
            return None
        except Exception as e:
            self.log.debug(f"Probe error for {url}: {type(e).__name__}: {e}")
            return None

    def enumerate_videos(self, url: str, limit: int = 0, site_hint: str = "") -> List[VideoRef]:
        """Return flat list of VideoRef from a user/channel/playlist URL.
        site_hint: the site name from sites.json, used when extractor_key is empty."""
        opts = self._common_opts()
        opts.update({
            "extract_flat": "in_playlist",
            "skip_download": True,
            "lazy_playlist": True,
        })
        if limit:
            opts["playlistend"] = limit

        videos: List[VideoRef] = []
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    return []
                info = ydl.sanitize_info(info)
                entries = info.get("entries") or [info]
                default_site = site_hint or info.get("extractor_key", "").lower() or "unknown"
                for e in entries:
                    if not e:
                        continue
                    vid = e.get("id") or ""
                    vurl = e.get("url") or e.get("webpage_url") or ""
                    if not vid:
                        continue
                    # If URL is missing, reconstruct from ie_key + id
                    if not vurl:
                        vurl = e.get("original_url") or ""
                    if not vurl:
                        continue
                    # Prefer ie_key (yt-dlp's extractor identifier) over our hint
                    site = (e.get("ie_key") or e.get("extractor_key") or "").lower()
                    if not site or site == "generic":
                        site = default_site
                    videos.append(VideoRef(
                        site=site,
                        video_id=vid,
                        video_url=vurl,
                        title=e.get("title") or vid,
                        uploader=e.get("uploader", "") or "",
                        uploader_id=e.get("uploader_id", "") or "",
                        duration=e.get("duration") or 0.0,
                    ))
        except Exception as e:
            self.log.warning(f"Enumeration failed for {url}: {e}")
        return videos

    def _download_opts(self, performer: str, output_dir: Path) -> dict:
        """Build opts for the actual download phase."""
        opts = self._common_opts()
        opts.update({
            "skip_download": False,
            "outtmpl": str(output_dir / performer / "%(extractor)s-%(id)s-%(title).100B.%(ext)s"),
            "writeinfojson": False,
            "writethumbnail": False,
            "continuedl": True,
            "overwrites": False,
            "nopart": False,
            "concurrent_fragment_downloads": 8,   # HLS/DASH fragments
        })
        if self.config.rate_limit:
            opts["ratelimit"] = self._parse_rate(self.config.rate_limit)
        if self.config.min_duration_seconds:
            from yt_dlp.utils import match_filter_func
            # "?" marks field as optional — videos with unknown duration pass
            opts["match_filter"] = match_filter_func(
                f"duration >=? {self.config.min_duration_seconds}"
            )
        # aria2c external downloader — best for direct HTTP (mp4).
        # Let yt-dlp handle HLS/DASH natively with concurrent fragments.
        if self.config.use_aria2c and ARIA2C_PATH:
            opts["external_downloader"] = {"default": ARIA2C_PATH}
            opts["external_downloader_args"] = {
                "default": [
                    "-x", str(self.config.aria2c_connections),
                    "-s", str(self.config.aria2c_connections),
                    "-k", "1M",
                    "--max-tries=3",
                    "--retry-wait=3",
                    "--connect-timeout=30",
                    "--timeout=60",
                    "--file-allocation=none",
                    "--allow-overwrite=true",
                    "--auto-file-renaming=false",
                    "--console-log-level=error",
                    "--summary-interval=0",
                ],
            }
        # ffmpeg path for HLS/DASH merging + post-processing
        if FFMPEG_PATH:
            opts["ffmpeg_location"] = str(Path(FFMPEG_PATH).parent)
        return opts

    def download(self, video: VideoRef, output_dir: Path,
                 progress_hook=None) -> Optional[dict]:
        """Download one video. Returns info_dict on success, None on failure.

        Optional `progress_hook` is passed through to yt-dlp's `progress_hooks`
        so the caller can surface byte/speed/ETA updates (UI progress bar)."""
        opts = self._download_opts(video.performer, output_dir)
        if progress_hook is not None:
            opts["progress_hooks"] = [progress_hook]
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(video.video_url, download=True)
                if not info:
                    return None
                return ydl.sanitize_info(info)
        except yt_dlp.utils.DownloadError as e:
            self.log.warning(f"  {video.site}/{video.video_id}: download failed: {e}")
            return None
        except Exception as e:
            self.log.warning(f"  {video.site}/{video.video_id}: unexpected error: {e}")
            return None

    @staticmethod
    def _parse_rate(rate: str) -> int:
        """Parse '500K' / '2M' -> bytes/sec."""
        rate = rate.strip().upper()
        if not rate:
            return 0
        multipliers = {"K": 1024, "M": 1024 * 1024, "G": 1024 * 1024 * 1024}
        if rate[-1] in multipliers:
            return int(float(rate[:-1]) * multipliers[rate[-1]])
        return int(rate)


# ── Universal Downloader ──────────────────────────────────────────────────────
class UniversalDownloader:
    def __init__(self, config: UniversalConfig, registry: SiteRegistry, log: logging.Logger):
        self.config = config
        self.registry = registry
        self.log = log
        self.output_dir = Path(config.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.history = DownloadHistory(self.output_dir / "history.json")
        self.failed = FailedHistory(self.output_dir / "failed.json")
        self.engine = YtdlpEngine(config, log)
        # Live progress tracker — the web UI tails downloads/_progress.json
        self.progress = ProgressTracker(self.output_dir)
        # Custom scrapers for cam-archive sites not supported by yt-dlp.
        # Pass cookies_file so sites requiring auth (Recu.me, camwhores.tv
        # private videos) can access protected content.
        site_credentials = {}
        if config.camsmut_username and config.camsmut_password:
            site_credentials["camsmut"] = {
                "username": config.camsmut_username,
                "password": config.camsmut_password,
            }
        # Respect enabled_sites for custom scrapers too. If enabled_sites is
        # empty (== "all"), pass None to load every registered scraper;
        # otherwise filter to the intersection.
        enabled = config.enabled_sites or None
        self.custom_scrapers: List[_CustomSiteScraper] = _load_custom_scrapers(
            log, enabled_names=enabled, cookies_file=config.cookies_file,
            site_credentials=site_credentials,
        )

    def check_disk_space(self) -> bool:
        try:
            free_gb = shutil.disk_usage(self.output_dir.resolve().anchor).free / (1024 ** 3)
        except Exception:
            return True
        if free_gb < self.config.min_disk_gb:
            self.log.error(f"DISK LOW: {free_gb:.1f} GB free (need {self.config.min_disk_gb})")
            return False
        return True

    # ── Probe phase: find which sites have the performer ─────────────────────
    def probe_all_sites(self, performer: str) -> List[ProbeHit]:
        """Probe every enabled site for this performer in parallel. For each site,
        we collect ALL successful probes, then pick the best URL (highest video
        count). This handles sites with multiple URL patterns like YouTube
        (/videos vs /streams vs /shorts) correctly."""
        sites = self.registry.enabled(self.config.enabled_sites)
        jobs: List[tuple] = []  # (site_config, url, pattern_index)
        for site in sites:
            for idx, pattern in enumerate(site.patterns):
                jobs.append((site, pattern.format(u=performer), idx))

        self.log.info(f"Probing {len(jobs)} URL patterns across {len(sites)} sites for '{performer}'")
        # Surface probe progress to the UI.
        self.progress.set_phase("probing", f"Probing {len(sites)} sites...")
        probe_counter = [0]   # mutable box for inner closure
        probe_lock = threading.Lock()

        min_entries = max(1, self.config.min_probe_entries)
        def probe_one(job):
            site, url, idx = job
            try:
                info = self.engine.probe(url)
            finally:
                with probe_lock:
                    probe_counter[0] += 1
                    self.progress.note_probe(site.name, probe_counter[0], len(jobs))
            if not info:
                return None
            entries = info.get("entries") or []
            entries_list = list(entries) if entries else []
            # Reject hits with fewer entries than threshold — most tube sites
            # return a 1-video "placeholder" for non-existent users (usually a
            # search-first-result or the site's promo video).
            if len(entries_list) < min_entries:
                return None
            return (site.name, idx, ProbeHit(
                site=site.name,
                url=url,
                entry_count=len(entries_list),
                uploader_id=info.get("uploader_id", "") or info.get("id", ""),
            ))

        # Collect ALL successful hits per site, then pick best one per site.
        # Hard wall-clock cap on the whole probe phase so we never hang.
        # Use explicit non-waiting shutdown: already-running probe threads
        # that are stuck in sockets become daemon threads and die with the
        # process — we don't block on them.
        per_site: Dict[str, List[tuple]] = {}   # site_name -> [(idx, ProbeHit), ...]
        max_probe_seconds = self.config.probe_timeout
        # Use daemon threads so stuck workers die with the process
        import concurrent.futures as _cf
        pool = _cf.ThreadPoolExecutor(
            max_workers=self.config.max_parallel_probes,
            thread_name_prefix="probe",
        )
        # Mark workers as daemon so they don't prevent interpreter shutdown.
        # (Patch the pool's thread factory before first submit.)
        _orig_adjust = pool._adjust_thread_count
        def _daemonize_workers():
            _orig_adjust()
            for t in pool._threads:
                if not t.daemon:
                    try:
                        t.daemon = True
                    except RuntimeError:
                        pass
        pool._adjust_thread_count = _daemonize_workers
        try:
            futs = {pool.submit(probe_one, j): j for j in jobs}
            try:
                for f in _cf.as_completed(futs, timeout=max_probe_seconds):
                    try:
                        r = f.result()
                    except Exception as e:
                        self.log.debug(f"Probe exception: {e}")
                        continue
                    if not r:
                        continue
                    site_name, idx, hit = r
                    per_site.setdefault(site_name, []).append((idx, hit))
            except _cf.TimeoutError:
                pending = sum(1 for f in futs if not f.done())
                self.log.warning(f"Probe phase timed out after {max_probe_seconds}s "
                                f"({pending} of {len(jobs)} probes still pending)")
        finally:
            # Don't wait for stuck probes — they're daemon threads.
            pool.shutdown(wait=False, cancel_futures=True)

        # For each site, pick the best hit: first by entry_count, then by pattern order
        # (pattern order = user's preference in sites.json)
        hits: List[ProbeHit] = []
        for site_name, candidates in per_site.items():
            # Sort by (-entry_count, idx) so higher count wins, tie broken by pattern order
            candidates.sort(key=lambda t: (-t[1].entry_count, t[0]))
            best = candidates[0][1]
            hits.append(best)
            self.progress.note_hit(best.site, best.entry_count)
            self.log.info(f"  HIT: {best.site} ({best.entry_count} videos) @ {best.url}")
            if len(candidates) > 1:
                others = [f"{c[1].url[:60]} ({c[1].entry_count})" for c in candidates[1:]]
                self.log.debug(f"    (also saw: {', '.join(others)})")

        hits.sort(key=lambda h: -h.entry_count)
        return hits

    # ── Custom scraper probe (parallel to yt-dlp probe) ──────────────────────
    def probe_custom_scrapers(self, performer: str) -> List[tuple]:
        """Probe all custom scrapers (with spelling variants). Returns
        list of (scraper, ProbeHit, variant_used) tuples, best hit per scraper."""
        variants = _username_variants(performer)
        self.log.info(f"Probing {len(self.custom_scrapers)} custom scrapers "
                     f"with {len(variants)} variants")

        jobs = []  # (scraper, variant)
        for scraper in self.custom_scrapers:
            for v in variants:
                jobs.append((scraper, v))

        # Track custom-scraper probe progress alongside yt-dlp's so the UI
        # reflects the full probe pipeline.
        cp_counter = [0]
        cp_lock = threading.Lock()
        total_probes = self.progress.session.get("probe_total", 0) + len(jobs)
        base_done = self.progress.session.get("probe_done", 0)
        with cp_lock:
            self.progress.set_phase("probing", f"Probing {len(self.custom_scrapers)} custom scrapers...")
            self.progress.note_probe("", base_done, total_probes)

        def _probe(job):
            scraper, variant = job
            try:
                hit = scraper.probe(variant)
            finally:
                with cp_lock:
                    cp_counter[0] += 1
                    self.progress.note_probe(
                        scraper.NAME, base_done + cp_counter[0], total_probes,
                    )
            try:
                if hit:
                    return (scraper, hit, variant)
            except Exception as e:
                self.log.debug(f"Custom probe {scraper.NAME}/{variant}: {e}")
            return None

        # Per-scraper best hit, with bias toward the exact username so we
        # don't grab a "Macy" generic search when "Macy2000" tag had 24 real hits.
        per_scraper: Dict[str, tuple] = {}  # scraper_name -> (scraper, hit, variant)

        def _score(hit, variant: str) -> float:
            """Score a hit. Exact-username hits get a 2x multiplier so /tags/Macy2000/
            with 24 beats /search/Macy/ with 59. Tag-URL hits also prefer tag matches."""
            count = hit.entry_count
            if variant.lower() == performer.lower():
                count *= 2.0
            # Prefer /tags/ over /search/ URLs (tag = precise, search = fuzzy)
            if "/tags/" in hit.url.lower():
                count *= 1.3
            elif "/search/" in hit.url.lower():
                count *= 0.8
            return count

        import concurrent.futures as _cf
        pool = _cf.ThreadPoolExecutor(max_workers=8, thread_name_prefix="cust-probe")
        # Make workers daemon so stuck threads die with process
        _orig = pool._adjust_thread_count
        def _daemonize():
            _orig()
            for t in pool._threads:
                if not t.daemon:
                    try: t.daemon = True
                    except RuntimeError: pass
        pool._adjust_thread_count = _daemonize
        try:
            futs = {pool.submit(_probe, j): j for j in jobs}
            deadline = time.time() + 60
            for f in _cf.as_completed(futs, timeout=90):
                try:
                    r = f.result()
                except Exception:
                    continue
                if not r:
                    continue
                scraper, hit, variant = r
                prev = per_scraper.get(scraper.NAME)
                # Use score (which factors in exact-username + tag preference)
                if prev is None or _score(hit, variant) > _score(prev[1], prev[2]):
                    per_scraper[scraper.NAME] = (scraper, hit, variant)
                if time.time() > deadline:
                    break
        except Exception as e:
            self.log.warning(f"Custom probe phase error: {e}")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        results = list(per_scraper.values())
        results.sort(key=lambda t: -t[1].entry_count)
        for scraper, hit, variant in results:
            tag = f" [variant:{variant}]" if variant != performer else ""
            self.log.info(f"  HIT: {hit.site} ({hit.entry_count} videos) @ {hit.url}{tag}")
            self.progress.note_hit(hit.site, hit.entry_count)
        return results

    def enumerate_custom(self, scraper: _CustomSiteScraper, hit, performer: str) -> List[VideoRef]:
        """Convert custom-scraper VideoRef (from custom_scrapers module) to our
        VideoRef type. Also filters videos by URL/slug to avoid false positives
        from overly-broad search results (e.g. /search/Macy/ matching 'Macy K',
        'Macy Cartel', 'Macy Meadows' etc. when we want Macy2000)."""
        max_v = max(hit.entry_count * 2, self.config.max_videos_per_site)
        max_v = min(max_v, 1000)
        try:
            vids = scraper.enumerate(hit, performer, limit=max_v)
        except Exception as e:
            self.log.warning(f"  {scraper.NAME} enumerate failed: {e}")
            return []

        # Filter: only keep videos whose URL/slug plausibly matches the performer.
        # This rejects "Macy K" / "Macy Cartel" / "Macy Meadows" when searching
        # for Macy2000, and "Blondie.Lilllie" / "Kjbennet-blondie-fuck" etc.
        # when searching for "blondie_254".
        #
        # Applied to ALL custom-scraper enumerations — even /tags/ hits, because
        # when a tag URL doesn't actually exist on the site, many KVS mirrors
        # serve a fallback search-result page that matches the substring only
        # (e.g. /tags/blondie_254/ returning every video tagged "blondie").
        filtered = []
        rejected = 0
        for cv in vids:
            slug_text = cv.video_url + " " + (cv.title or "") + " " + (cv.uploader or "")
            if not custom_scrapers.video_title_matches_user(slug_text, performer):
                rejected += 1
                self.log.debug(f"  [{scraper.NAME}] reject '{performer}' mismatch: "
                               f"{(cv.title or cv.video_id)[:60]} @ {cv.video_url[:80]}")
                continue
            filtered.append(cv)
        if rejected:
            self.log.info(f"  {scraper.NAME}: filtered {rejected} off-topic results "
                         f"(URL/title didn't match '{performer}')")
        vids = filtered

        out: List[VideoRef] = []
        for cv in vids:
            out.append(VideoRef(
                site=cv.site,
                video_id=cv.video_id,
                video_url=cv.video_url,
                title=cv.title,
                uploader=cv.uploader,
                uploader_id=cv.uploader_id,
                duration=cv.duration or 0.0,
                performer=performer,
                stream_url="",    # populated later on demand
                stream_kind="",
                stream_headers={},
                is_custom=True,
            ))
        return out

    # ── Enumerate phase: get video list per hit ──────────────────────────────
    def enumerate_for_hit(self, hit: ProbeHit, performer: str) -> List[VideoRef]:
        max_v = self.config.max_videos_per_site * 3  # fetch extra to allow for already-downloaded
        videos = self.engine.enumerate_videos(hit.url, limit=max_v, site_hint=hit.site)
        for v in videos:
            v.performer = performer
            if not v.site or v.site == "unknown":
                v.site = hit.site
        return videos

    # KVS-family sites share identical video IDs across mirrors. When the same
    # video_id is already downloaded on one KVS site, we don't need to fetch it
    # from another mirror.
    KVS_FAMILY = {
        "camwhores_tv", "camwhores_video", "camwhores_co", "camwhoreshd",
        "camwhoresbay", "camwhoresbay_tv", "camwhores_bz", "camwhorescloud",
        "camvideos_tv", "camhub_cc", "camwh_com", "cambro_tv", "camstreams_tv",
        "porntrex",
    }

    def _is_already_downloaded_cross_site(self, performer: str, v: VideoRef) -> bool:
        """Check if this video_id has already been downloaded from any KVS mirror."""
        if v.site not in self.KVS_FAMILY:
            return False
        perf_data = self.history.data.get(performer.lower(), {})
        for gid in perf_data.keys():
            if "|" not in gid:
                continue
            other_site, other_vid = gid.split("|", 1)
            if other_vid == v.video_id and other_site in self.KVS_FAMILY:
                return True
        return False

    def _is_already_failed_cross_site(self, v: VideoRef) -> bool:
        """Check if this video_id is permanently-failed on any KVS mirror."""
        if v.site not in self.KVS_FAMILY:
            return False
        for gid, entry in self.failed.data.items():
            if "|" not in gid:
                continue
            other_site, other_vid = gid.split("|", 1)
            if other_vid == v.video_id and other_site in self.KVS_FAMILY:
                if entry.get("permanent", False):
                    return True
        return False

    def filter_new(self, performer: str, videos: List[VideoRef]) -> tuple[list, dict]:
        """Return (new_videos, counts_dict). counts has 'downloaded', 'failed', 'new'.

        Also does cross-site deduplication for KVS mirrors (camwhores.tv/.video/.bz/bay.tv/etc
        share video IDs, so fetching from each mirror is redundant)."""
        new, dled, failed = [], 0, 0
        max_new = self.config.max_videos_per_site
        for v in videos:
            if self.history.is_downloaded(performer, v.global_id):
                dled += 1
                continue
            if self._is_already_downloaded_cross_site(performer, v):
                dled += 1
                continue
            if self.failed.is_permanently_failed(v.global_id):
                failed += 1
                continue
            if self._is_already_failed_cross_site(v):
                failed += 1
                continue
            new.append(v)
            if not getattr(v, "is_custom", False) and len(new) >= max_new:
                break
        if new and any(getattr(v, "is_custom", False) for v in new):
            # Cap at reasonable number to avoid 1000s of videos on broad searches
            new = new[: max_new * 3]
        return new, {"downloaded": dled, "failed": failed, "new": len(new)}

    # ── Download phase ───────────────────────────────────────────────────────
    def _sanitize_filename(self, name: str) -> str:
        """Windows-safe filename: strip disallowed chars."""
        for ch in '<>:"/\\|?*':
            name = name.replace(ch, "_")
        name = re.sub(r"\s+", " ", name).strip()
        # Limit length
        return name[:180] if len(name) > 180 else name

    def _download_custom_mp4(self, v: VideoRef, out_path: Path,
                             progress_slot: Optional[int] = None) -> bool:
        """Download a direct MP4 URL. Tries aria2c first, falls back to curl
        if aria2c fails (some CDNs don't play well with aria2c's TLS)."""
        ok = False
        if ARIA2C_PATH:
            ok = self._download_via_aria2c(v, out_path, progress_slot=progress_slot)
        if not ok:
            # aria2c failed OR not available — try curl
            if out_path.exists():
                try: out_path.unlink()
                except Exception: pass
            ok = self._download_with_curl(v, out_path, progress_slot=progress_slot)
        return ok

    # Regex for aria2c's status summary line (emitted once per summary-interval):
    #   [#abcd12 12MiB/100MiB(12%) CN:16 DL:2.5MiB ETA:30s]
    _ARIA2C_LINE = re.compile(
        r"\[#\S+\s+(?P<done>[\d.]+)(?P<done_unit>[KMGT]i?B)"
        r"/(?P<total>[\d.]+)(?P<total_unit>[KMGT]i?B)"
        r"\((?P<pct>[\d.]+)%\)"
        r"(?:[^]]*DL:(?P<speed>[\d.]+)(?P<speed_unit>[KMGT]i?B))?"
        r"(?:[^]]*ETA:(?P<eta>[\dhms]+))?",
        re.IGNORECASE,
    )

    @staticmethod
    def _bytes_from(num_str: str, unit: str) -> int:
        try:
            n = float(num_str)
        except ValueError:
            return 0
        u = unit.upper().rstrip("B").rstrip("I")
        mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}.get(u, 1)
        return int(n * mult)

    @staticmethod
    def _parse_eta(tok: str) -> int:
        """aria2c prints ETA like '30s' / '2m30s' / '1h20m'."""
        if not tok:
            return 0
        total = 0
        cur = ""
        for ch in tok.lower():
            if ch.isdigit():
                cur += ch
            else:
                try:
                    v = int(cur)
                except ValueError:
                    v = 0
                cur = ""
                if ch == "h":   total += v * 3600
                elif ch == "m": total += v * 60
                elif ch == "s": total += v
        if cur:
            try: total += int(cur)
            except ValueError: pass
        return total

    def _download_via_aria2c(self, v: VideoRef, out_path: Path,
                             progress_slot: Optional[int] = None) -> bool:
        cmd = [
            ARIA2C_PATH,
            "-x", str(self.config.aria2c_connections),
            "-s", str(self.config.aria2c_connections),
            "-k", "1M",
            "--max-tries=3", "--retry-wait=2",
            "--connect-timeout=30", "--timeout=120",
            "--file-allocation=none",
            "--allow-overwrite=true", "--auto-file-renaming=false",
            "--console-log-level=error",
            # Emit a summary line every second so we can parse progress.
            # (Old value was 0 = disabled.)
            "--summary-interval=1" if progress_slot is not None else "--summary-interval=0",
            "--check-certificate=false",
            f"--user-agent={v.stream_headers.get('User-Agent', custom_scrapers.USER_AGENT)}",
        ]
        ref = v.stream_headers.get("Referer", "")
        if ref:
            cmd.append(f"--referer={ref}")
        for k, val in v.stream_headers.items():
            if k.lower() in ("referer", "user-agent"):
                continue
            cmd += ["--header", f"{k}: {val}"]
        cmd += ["-d", str(out_path.parent), "-o", out_path.name, v.stream_url]
        try:
            if progress_slot is None:
                r = subprocess.run(cmd, capture_output=True, timeout=3600)
                rc = r.returncode
            else:
                # Streaming mode — read stdout line-by-line and push to UI.
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                )
                deadline = time.time() + 3600
                assert proc.stdout is not None
                for line in proc.stdout:
                    if time.time() > deadline:
                        proc.kill()
                        break
                    m = self._ARIA2C_LINE.search(line)
                    if m:
                        done = self._bytes_from(m.group("done"), m.group("done_unit"))
                        total = self._bytes_from(m.group("total"), m.group("total_unit"))
                        speed = self._bytes_from(m.group("speed") or "0",
                                                 m.group("speed_unit") or "B")
                        eta = self._parse_eta(m.group("eta") or "")
                        try: pct = float(m.group("pct"))
                        except (TypeError, ValueError): pct = 0.0
                        self.progress.update_video(progress_slot,
                                                   bytes_done=done,
                                                   bytes_total=total,
                                                   percent=pct,
                                                   speed_bps=speed,
                                                   eta_seconds=eta)
                rc = proc.wait()
                r = None

            ok = rc == 0 and out_path.exists() and out_path.stat().st_size > 100_000
            if not ok and r is not None:
                stderr = (r.stderr.decode(errors="replace") if r.stderr else "")
                stdout = (r.stdout.decode(errors="replace") if r.stdout else "")
                tail = (stderr or stdout)[-400:].replace("\n", " | ")
                size = out_path.stat().st_size if out_path.exists() else 0
                self.log.debug(
                    f"aria2c exit={rc} size={size} url={v.stream_url[:80]}... "
                    f"msg={tail}"
                )
            return ok
        except subprocess.TimeoutExpired:
            self.log.debug(f"aria2c timeout: {v.site}/{v.video_id}")
            return False
        except Exception as e:
            self.log.debug(f"aria2c error: {e}")
            return False

    def _download_with_curl(self, v: VideoRef, out_path: Path,
                            progress_slot: Optional[int] = None) -> bool:
        """Fallback MP4 download via curl. Single connection but more robust to
        CDN quirks that confuse aria2c (TLS fingerprinting, IPv6 issues, etc.)."""
        cmd = [
            "curl", "-L", "--fail", "--retry", "3", "--retry-delay", "3",
            "--connect-timeout", "30", "--max-time", "3600",
            "--speed-limit", "1024", "--speed-time", "60",
            "-H", f"User-Agent: {v.stream_headers.get('User-Agent', custom_scrapers.USER_AGENT)}",
        ]
        ref = v.stream_headers.get("Referer", "")
        if ref:
            cmd += ["-H", f"Referer: {ref}"]
        for k, val in v.stream_headers.items():
            if k.lower() in ("referer", "user-agent"):
                continue
            cmd += ["-H", f"{k}: {val}"]
        cmd += ["-o", str(out_path), v.stream_url]

        # If tracking progress, start a background thread to poll out_path's
        # size while curl runs — curl's own --progress-bar is terse and hard to
        # parse, and we already know the total size from the Content-Length.
        stop_flag = [False]
        if progress_slot is not None:
            def _poll():
                last_bytes = 0
                last_time = time.time()
                while not stop_flag[0]:
                    time.sleep(0.5)
                    try:
                        size = out_path.stat().st_size if out_path.exists() else 0
                    except OSError:
                        size = 0
                    now = time.time()
                    dt = max(now - last_time, 0.001)
                    speed = int((size - last_bytes) / dt) if size > last_bytes else 0
                    last_bytes, last_time = size, now
                    self.progress.update_video(progress_slot,
                                               bytes_done=size,
                                               speed_bps=speed)
            threading.Thread(target=_poll, daemon=True).start()

        try:
            r = subprocess.run(cmd, capture_output=True, timeout=3600)
            ok = r.returncode == 0 and out_path.exists() and out_path.stat().st_size > 100_000
            if not ok:
                stderr = (r.stderr.decode(errors="replace") if r.stderr else "")[-200:]
                self.log.debug(f"curl exit={r.returncode} size={out_path.stat().st_size if out_path.exists() else 0} msg={stderr}")
            return ok
        except Exception as e:
            self.log.debug(f"curl error: {e}")
            return False
        finally:
            stop_flag[0] = True

    def _download_custom_hls(self, v: VideoRef, out_path: Path) -> bool:
        """Download HLS m3u8 via ffmpeg."""
        if not FFMPEG_PATH:
            self.log.warning("ffmpeg not found — can't download HLS")
            return False
        headers_str = ""
        for k, val in v.stream_headers.items():
            headers_str += f"{k}: {val}\r\n"
        cmd = [
            FFMPEG_PATH, "-hide_banner", "-loglevel", "error",
            "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_delay_max", "30",
        ]
        if headers_str:
            cmd += ["-headers", headers_str]
        cmd += [
            "-i", v.stream_url,
            "-c", "copy", "-bsf:a", "aac_adtstoasc",
            "-movflags", "+faststart",
            "-y", str(out_path),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=3600)
            return r.returncode == 0 and out_path.exists() and out_path.stat().st_size > 100_000
        except Exception as e:
            self.log.debug(f"ffmpeg error: {e}")
            return False

    def _download_custom_video(self, v: VideoRef) -> tuple:
        """Download a VideoRef that came from a custom scraper.
        Extracts stream URL on demand, then uses aria2c (mp4) or ffmpeg (hls)."""
        # Resolve stream URL if not already
        if not v.stream_url:
            # Find the scraper for this site
            scraper = next((s for s in self.custom_scrapers if s.NAME == v.site), None)
            if not scraper:
                self.failed.record_failure(v, f"no custom scraper for {v.site}", 0)
                return ("fail", v, None)
            # Create a lightweight custom_scrapers.VideoRef to pass in
            cv = custom_scrapers.VideoRef(
                site=v.site, video_id=v.video_id, video_url=v.video_url,
                title=v.title, performer=v.performer,
            )
            try:
                ok = scraper.extract_stream(cv)
            except Exception as e:
                self.log.debug(f"  {v.site}/{v.video_id} extract_stream exception: {e}")
                ok = False
            if not ok or not cv.stream_url:
                # Private/members-only videos: mark permanent so we don't keep
                # hitting them on every run (they can't be bypassed without auth).
                if cv.stream_kind == "private":
                    # Set fail_count high to force permanent flag
                    for _ in range(3):
                        self.failed.record_failure(v, "private/members-only (404 equivalent)", 0)
                    self.log.info(f"  PRIVATE: {v.site}/{v.video_id}: {cv.title[:60] if cv.title else v.title[:60]}")
                    return ("skip", v, None)
                self.failed.record_failure(v, "stream extraction failed", 0)
                self.log.warning(f"  FAIL extract: {v.site}/{v.video_id}: {v.title[:60]}")
                return ("fail", v, None)
            v.stream_url = cv.stream_url
            v.stream_kind = cv.stream_kind
            v.stream_headers = cv.stream_headers
            if cv.title and not v.title:
                v.title = cv.title

        # Build output path
        perf_dir = self.output_dir / v.performer
        perf_dir.mkdir(parents=True, exist_ok=True)
        safe_title = self._sanitize_filename(v.title or v.video_id)
        out_path = perf_dir / f"{v.site}-{v.video_id}-{safe_title}.mp4"

        # Download — wrap with progress tracking so the UI can show a live bar.
        backend = "ffmpeg" if v.stream_kind == "hls" else ("aria2c" if ARIA2C_PATH else "curl")
        slot = self.progress.start_video(
            site=v.site, video_id=v.video_id,
            title=v.title or v.video_id, backend=backend,
        )
        try:
            if v.stream_kind == "hls":
                ok = self._download_custom_hls(v, out_path)
            else:
                ok = self._download_custom_mp4(v, out_path, progress_slot=slot)
        finally:
            # Flash 100 % on success so the bar visually completes
            if ok and out_path.exists():
                fsz = out_path.stat().st_size
                self.progress.update_video(slot, bytes_done=fsz, bytes_total=fsz, percent=100.0)
            self.progress.finish_video(slot, status="ok" if ok else "fail")

        if not ok:
            self.failed.record_failure(v, f"{v.stream_kind or 'mp4'} download failed", 0)
            self.log.warning(f"  FAIL dl: {v.site}/{v.video_id}: {v.title[:60]}")
            return ("fail", v, None)

        filesize = out_path.stat().st_size
        if filesize < 100_000:
            self.failed.record_failure(v, "file too small", filesize)
            self.log.warning(f"  SKIP: {v.site}/{v.video_id} too small ({filesize} bytes)")
            try: out_path.unlink()
            except Exception: pass
            return ("skip", v, None)

        self.history.mark_downloaded(v, output_path=str(out_path), filesize=filesize)
        self.log.info(f"  OK ({filesize / 1024 / 1024:.1f} MB): {out_path.name}")
        return ("ok", v, str(out_path))

    def download_videos(self, videos: List[VideoRef]) -> dict:
        stats = {"ok": 0, "fail": 0, "skip": 0}
        if not videos:
            return stats

        def _dl_one(v: VideoRef):
            if not self.check_disk_space():
                self.log.error("Disk full — aborting downloads.")
                return None
            # Custom-scraper videos use their own download path
            if v.is_custom:
                return self._download_custom_video(v)
            slot = self.progress.start_video(
                site=v.site, video_id=v.video_id,
                title=v.title or v.video_id, backend="yt-dlp",
            )
            try:
                info = self.engine.download(
                    v, self.output_dir,
                    progress_hook=make_yt_dlp_hook(self.progress, slot),
                )
            finally:
                # Always clean up the active slot; finish counters happen below
                # via session_increment
                self.progress._active.pop(slot, None)
                self.progress._flush()
            if not info:
                self.failed.record_failure(v, "yt-dlp download failed", 0)
                self.log.warning(f"  FAIL: {v.site}/{v.video_id}: {v.title[:60]}")
                return ("fail", v, None)

            # Locate resulting file
            output = ""
            if info.get("requested_downloads"):
                output = info["requested_downloads"][0].get("filepath", "")
            if not output:
                output = info.get("_filename") or info.get("filename") or ""

            filesize = 0
            if output and Path(output).exists():
                filesize = Path(output).stat().st_size

            # Real failure: yt-dlp returned info but file doesn't exist or is empty.
            # This typically means the match_filter rejected it, or extraction
            # succeeded but download silently failed.
            if filesize < 1024:
                reason = "downloaded file missing or too small"
                if info.get("_filter_blocked"):
                    reason = "filtered out (duration/condition)"
                self.failed.record_failure(v, reason, filesize)
                self.log.warning(f"  SKIP: {v.site}/{v.video_id} ({reason}): {v.title[:60]}")
                return ("skip", v, None)

            self.history.mark_downloaded(v, output_path=output, filesize=filesize)
            size_mb = filesize / (1024 * 1024)
            self.log.info(f"  OK ({size_mb:.1f} MB): {Path(output).name if output else v.title}")
            return ("ok", v, output)

        # Per-(performer, site) success cap — private/failed videos don't
        # count against the per-run budget.
        max_per_site = self.config.max_videos_per_site
        site_success: Dict[str, int] = {}  # "performer|site" -> count

        def should_skip(v: VideoRef) -> bool:
            key = f"{v.performer}|{v.site}"
            return site_success.get(key, 0) >= max_per_site

        # Sequentially per performer/site to respect the per-site cap.
        # Group videos by (performer, site) for parallelism within a group.
        from collections import defaultdict
        groups: Dict[tuple, List[VideoRef]] = defaultdict(list)
        for v in videos:
            groups[(v.performer, v.site)].append(v)

        for (performer, site), site_videos in groups.items():
            site_key = f"{performer}|{site}"
            # Process sequentially per site so we can stop at max_per_site successes
            for v in site_videos:
                if site_success.get(site_key, 0) >= max_per_site:
                    break
                if not self.check_disk_space():
                    self.log.error("Disk full — aborting downloads.")
                    return stats
                try:
                    r = _dl_one(v)
                except Exception as e:
                    self.log.warning(f"Download exception {v.site}/{v.video_id}: {e}")
                    stats["fail"] += 1
                    continue
                if r is None:
                    stats["skip"] += 1
                    self.progress.session_increment("skip")
                    continue
                status, rv, _ = r
                stats[status] += 1
                self.progress.session_increment(status)
                if status == "ok":
                    site_success[site_key] = site_success.get(site_key, 0) + 1
        return stats

    # ── High-level orchestration ─────────────────────────────────────────────
    def run_performer(self, performer: str, dry_run: bool = False) -> dict:
        summary = {"performer": performer, "hits": 0, "new_videos": 0, "downloaded": 0, "failed": 0}

        # Surface to the UI: who's being processed right now
        self.progress.session_start(performer, total_queued=0)

        if HAVE_RICH:
            console.rule(f"[bold cyan]{performer}[/bold cyan]")

        # Probe yt-dlp sites + custom scrapers in parallel (well, sequential
        # but both populate the hits list)
        yt_hits = self.probe_all_sites(performer)
        custom_hits = self.probe_custom_scrapers(performer)   # [(scraper, hit, variant), ...]

        total_hits = len(yt_hits) + len(custom_hits)
        summary["hits"] = total_hits
        if total_hits == 0:
            self.log.warning(f"No sites returned videos for '{performer}'")
            self.progress.session_end()
            return summary

        self.log.info(f"Found {total_hits} site hits for '{performer}' "
                     f"({len(yt_hits)} yt-dlp + {len(custom_hits)} custom):")
        if HAVE_RICH:
            t = Table(title=f"Sites with videos for {performer}", show_lines=False)
            t.add_column("Site", style="cyan")
            t.add_column("Source", style="dim")
            t.add_column("Videos", justify="right", style="green")
            t.add_column("URL")
            for h in yt_hits:
                t.add_row(h.site, "yt-dlp", str(h.entry_count), h.url[:70])
            for scraper, h, variant in custom_hits:
                tag = f" [{variant}]" if variant != performer else ""
                t.add_row(h.site, "custom", str(h.entry_count), h.url[:70] + tag)
            console.print(t)

        self.progress.set_phase("enumerating",
                                 f"Enumerating {total_hits} site hits...")

        all_new: List[VideoRef] = []
        # Enumerate yt-dlp hits
        for hit in yt_hits:
            videos = self.enumerate_for_hit(hit, performer)
            new, counts = self.filter_new(performer, videos)
            self.log.info(f"  {hit.site}: {counts['new']} new + {counts['downloaded']} downloaded + "
                         f"{counts['failed']} failed (scanned {len(videos)})")
            all_new.extend(new)

        # Enumerate custom-scraper hits
        for scraper, hit, variant in custom_hits:
            videos = self.enumerate_custom(scraper, hit, performer)
            new, counts = self.filter_new(performer, videos)
            self.log.info(f"  {hit.site}: {counts['new']} new + {counts['downloaded']} downloaded + "
                         f"{counts['failed']} failed (scanned {len(videos)})")
            all_new.extend(new)

        # Cross-mirror dedup: for KVS family, keep only ONE VideoRef per video_id.
        # Mirrors (camwhores.tv/.video/.bz/bay.tv/etc) share the same content, so
        # downloading from all three would waste bandwidth on identical videos.
        seen_ids: set = set()
        deduped: List[VideoRef] = []
        dupe_count = 0
        for v in all_new:
            if v.site in self.KVS_FAMILY:
                if v.video_id in seen_ids:
                    dupe_count += 1
                    continue
                seen_ids.add(v.video_id)
            deduped.append(v)
        if dupe_count:
            self.log.info(f"  Cross-mirror dedup: dropped {dupe_count} duplicate video IDs "
                         f"across KVS-family mirrors ({len(all_new)} -> {len(deduped)} videos)")
        all_new = deduped

        summary["new_videos"] = len(all_new)
        self.progress.session_update(total_queued=len(all_new))
        if dry_run:
            self.log.info(f"[dry-run] would download {len(all_new)} videos")
            if HAVE_RICH and all_new:
                t = Table(title=f"Videos to download ({len(all_new)})")
                t.add_column("#", style="dim", width=4)
                t.add_column("Site", style="cyan")
                t.add_column("ID", style="dim")
                t.add_column("Title", max_width=60)
                for i, v in enumerate(all_new[:30], 1):
                    t.add_row(str(i), v.site, v.video_id[:12], v.title[:60])
                if len(all_new) > 30:
                    t.add_row("...", "", "", f"... +{len(all_new) - 30} more")
                console.print(t)
            self.progress.session_end()
            return summary

        if not all_new:
            self.log.info(f"Nothing new to download for '{performer}'")
            self.progress.session_end()
            return summary

        self.progress.set_phase("downloading",
                                 f"Downloading {len(all_new)} videos...")
        stats = self.download_videos(all_new)
        summary["downloaded"] = stats["ok"]
        summary["failed"] = stats["fail"]
        self.log.info(f"'{performer}' done: {stats['ok']} OK, {stats['fail']} failed, {stats['skip']} skipped")
        self.progress.set_phase("done", f"Done: {stats['ok']} OK, {stats['fail']} failed")
        self.progress.session_end()
        return summary

    def run_all(self, dry_run: bool = False) -> None:
        if not self.config.performers:
            self.log.error("No performers configured. Edit config.json.")
            return
        totals = {"performers": 0, "hits": 0, "new_videos": 0, "downloaded": 0, "failed": 0}
        for p in self.config.performers:
            s = self.run_performer(p, dry_run=dry_run)
            totals["performers"] += 1
            for k in ("hits", "new_videos", "downloaded", "failed"):
                totals[k] += s.get(k, 0)
            if not self.check_disk_space():
                self.log.error("Out of disk space — stopping run.")
                break
        if HAVE_RICH:
            console.rule("[bold green]Run Complete[/bold green]")
            t = Table(title="Totals")
            for k, v in totals.items():
                t.add_row(k, str(v))
            console.print(t)


# ── CLI ──────────────────────────────────────────────────────────────────────
def list_supported_sites(registry: SiteRegistry) -> None:
    if not HAVE_RICH:
        for name, site in registry.sites.items():
            print(f"{name} [{site.category}] — {len(site.patterns)} patterns")
        return
    by_cat: Dict[str, List[SiteConfig]] = {}
    for site in registry.sites.values():
        by_cat.setdefault(site.category, []).append(site)
    for cat, sites in sorted(by_cat.items()):
        t = Table(title=f"[bold]{cat}[/bold]", show_lines=False)
        t.add_column("Name", style="cyan")
        t.add_column("Patterns", justify="right", style="green")
        t.add_column("Extractor", style="dim")
        t.add_column("Notes", max_width=50)
        for s in sorted(sites, key=lambda x: x.name):
            t.add_row(s.name, str(len(s.patterns)), s.yt_dlp_extractor, s.notes)
        console.print(t)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Universal video downloader — yt-dlp powered, multi-site",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("performer", nargs="?", help="Performer/username to download")
    parser.add_argument("--all", action="store_true",
                        help="Run for all performers in config.json")
    parser.add_argument("--sites", help="Comma-separated list of site names (overrides config)")
    parser.add_argument("--config", default=str(SCRIPT_DIR / "config.json"),
                        help="Path to config.json")
    parser.add_argument("--sites-file", default=str(SCRIPT_DIR / "sites.json"),
                        help="Path to sites.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="Probe & enumerate only; don't download")
    parser.add_argument("--list-sites", action="store_true",
                        help="List all supported sites and exit")
    parser.add_argument("--save-config", action="store_true",
                        help="Write default config.json template and exit")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Verbose console output (debug level)")
    args = parser.parse_args()

    cfg_path = Path(args.config)
    cfg = UniversalConfig.load(cfg_path)
    if args.verbose:
        cfg.verbose = True

    if args.save_config:
        # Populate a sensible default
        if not cfg.performers:
            cfg.performers = ["example_username"]
        cfg.save(cfg_path)
        print(f"Config template saved to: {cfg_path}")
        return 0

    log = setup_logging(Path(cfg.output_dir), verbose=cfg.verbose)

    if HAVE_RICH:
        engine_info = f"aria2c: {'yes' if ARIA2C_PATH else 'no'} | ffmpeg: {'yes' if FFMPEG_PATH else 'no'}"
        yt_ver = getattr(yt_dlp.version, "__version__", "?")
        console.print(Panel.fit(
            f"[bold cyan]Universal Video Downloader[/bold cyan]\n"
            f"  yt-dlp     : {yt_ver}\n"
            f"  Engine     : {engine_info}\n"
            f"  Config     : {cfg_path}\n"
            f"  Output     : {cfg.output_dir}\n"
            f"  Performers : {len(cfg.performers)} in config\n"
            f"  Concurrency: probe={cfg.max_parallel_probes} dl={cfg.max_parallel_downloads}\n"
            f"  History    : {DownloadHistory(Path(cfg.output_dir) / 'history.json').count()} downloaded",
        ))

    registry = SiteRegistry(Path(args.sites_file), log)
    if not registry.sites:
        log.error("No sites loaded. Check sites.json.")
        return 1

    if args.list_sites:
        list_supported_sites(registry)
        return 0

    # Override enabled sites from CLI
    if args.sites:
        cfg.enabled_sites = [s.strip() for s in args.sites.split(",") if s.strip()]
        log.info(f"Site filter (CLI): {cfg.enabled_sites}")

    dl = UniversalDownloader(cfg, registry, log)

    if args.all:
        dl.run_all(dry_run=args.dry_run)
    elif args.performer:
        dl.run_performer(args.performer, dry_run=args.dry_run)
    else:
        parser.print_help()
        return 1
    return 0


if __name__ == "__main__":
    rc = 0
    try:
        rc = main()
    except KeyboardInterrupt:
        rc = 130
    except SystemExit as e:
        # argparse --help / --list-sites exit via SystemExit; honor their code
        rc = int(e.code) if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        # Force-exit to kill any stuck probe daemon threads / curl_cffi workers
        # that might otherwise keep the interpreter alive.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(rc if rc is not None else 0)
