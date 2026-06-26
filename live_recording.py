#!/usr/bin/env python3
"""
Live cam recording integration for Harvestr.

Uses the vendored StreaMonitor backend at live_backend/streamonitor/ for
18 cam-site modules (Chaturbate, StripChat, CamSoda, Cam4, BongaCams,
Flirt4Free, Cherry.tv, Streamate, MyFreeCams, ManyVids, FanslyLive,
AmateurTV, CamsCom, DreamCam, SexChatHu, XloveCam, plus VR variants).

StreaMonitor (https://github.com/lossless1/StreaMonitor) is GPL-3.0;
see live_backend/LICENSE and live_backend/NOTICE.md.

This module:
  - Adds live_backend/ to sys.path and imports Bot, Status, site classes
  - Provides a LiveManager class for the web UI:
        add_model, remove_model, start_model, stop_model,
        get_status_snapshot, get_sites
  - Persists the model list to downloads/live_models.json (schema
    matches StreaMonitor's own config.json)
  - Runs each model as a daemon thread via StreaMonitor's Bot.restart()

Design notes:
  - The 19 site extractors (200-500 lines each of careful reverse-
    engineering) are NOT re-implemented — vendored verbatim.
  - If HARVESTR_STREAMONITOR env var is set, that path wins over the
    vendored copy (lets you test with a development checkout).
  - Recording output goes to <downloads>/<performer> [SITE]/N.mkv,
    matching StreaMonitor's layout exactly.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("harvestr.live")

# ── Discovery ────────────────────────────────────────────────────────────────
# Preference order:
#   1. HARVESTR_STREAMONITOR env var (dev override)
#   2. Vendored copy under live_backend/ (default — ships with Harvestr)
#   3. Common external install paths (fallback for users who cloned it manually)
_HERE = Path(__file__).resolve().parent
_VENDORED = _HERE / "live_backend"

_CANDIDATES = [
    os.environ.get("HARVESTR_STREAMONITOR", ""),
    str(_VENDORED),                    # vendored (the common case)
    r"C:\F\StreaMonitor",              # external install on Windows
    r"D:\F\StreaMonitor",
    str(Path.home() / "StreaMonitor"),
    str(Path.home() / "Documents" / "StreaMonitor"),
]

_STREAMONITOR_PATH: Optional[str] = None
for _cand in _CANDIDATES:
    if _cand and (Path(_cand) / "streamonitor" / "bot.py").exists():
        _STREAMONITOR_PATH = _cand
        break


# ── Try to import the Bot framework ──────────────────────────────────────────

available = False
import_error: Optional[str] = None
Bot = None            # type: ignore
RoomIdBot = None      # type: ignore
Status = None         # type: ignore
SITES: Dict[str, type] = {}   # "Chaturbate" -> Chaturbate class

if _STREAMONITOR_PATH:
    try:
        if _STREAMONITOR_PATH not in sys.path:
            sys.path.insert(0, _STREAMONITOR_PATH)
        # ── streamer-list config path (2026-05-09 fix) ──
        # StreaMonitor's `streamonitor/config.py` originally hardcodes
        # `config_loc = "config.json"` (relative). When Harvestr launches
        # from `universal/`, that resolves to `universal/config.json` —
        # which is the universal harvester's OWN config (a dict, not a
        # streamer list), so loadStreamers() ends up with 0 streamers
        # and the Live tab shows "0 MODELS TRACKED" even though the
        # user has hundreds of streamers configured.
        # Fix: pin StreaMonitor's config to a distinct absolute path
        # next to the StreaMonitor module so it never collides. We
        # prefer the user's existing data files in priority order:
        #   1. STRMNTR_CONFIG_PATH already set by caller (no override)
        #   2. <streamonitor_root>/config.json (the canonical location —
        #      typically D:\F\StreaMonitor\config.json or
        #      C:\F\StreaMonitor\config.json for users with an external
        #      install; live_backend/config.json for vendored)
        if not os.environ.get("STRMNTR_CONFIG_PATH"):
            _stream_cfg = Path(_STREAMONITOR_PATH) / "config.json"
            os.environ["STRMNTR_CONFIG_PATH"] = str(_stream_cfg)
            log.info(f"  [live] StreaMonitor config: {_stream_cfg}")
        # Separate Live recordings from Archive downloads. Archive files
        # go to <output_dir>/<performer>/..., live recordings to
        # <live_output_dir>/<performer> [SITE]/N.mkv. By default
        # live_output_dir = <output_dir>/_live, but the user can override
        # it in the Live settings modal to put recordings on a different
        # drive (e.g. a secondary disk with more space for long streams).
        _LIVE_DEFAULT = Path(__file__).resolve().parent / "downloads" / "_live"
        _LIVE_DIR = _LIVE_DEFAULT
        _user_live: str = ""
        try:
            _cfg_path_early = Path(__file__).resolve().parent / "config.json"
            if _cfg_path_early.exists():
                _cfg_early = json.loads(_cfg_path_early.read_text(encoding="utf-8"))
                _user_live = (_cfg_early.get("live") or {}).get("live_output_dir") or ""
                if _user_live:
                    _LIVE_DIR = Path(_user_live).expanduser()
        except Exception:
            pass
        # Try the configured live dir, but fall back to the default if it's
        # unreachable (e.g. D:\ no longer mounted). Otherwise StreaMonitor's
        # bots loop forever on FileNotFoundError when they try to record.
        try:
            _LIVE_DIR.mkdir(parents=True, exist_ok=True)
        except (OSError, FileNotFoundError) as _mk_err:
            log.warning(
                f"  [live] configured live_output_dir is unreachable "
                f"({_user_live!r}: {_mk_err}); falling back to {_LIVE_DEFAULT}"
            )
            _LIVE_DIR = _LIVE_DEFAULT
            try:
                _LIVE_DIR.mkdir(parents=True, exist_ok=True)
            except Exception as _e2:
                # If even the default can't be created (extremely unusual),
                # we still want StreaMonitor to import — it'll just error
                # at recording-time per-bot rather than tearing the whole
                # Live subsystem down.
                log.warning(f"  [live] default live dir also failed: {_e2}")
        os.environ["STRMNTR_DOWNLOAD_DIR"] = str(_LIVE_DIR)
        # Apply Live settings from config.json (read BEFORE import so
        # parameters.py sees them). These map to StreaMonitor's env hooks.
        try:
            _cfg_path = Path(__file__).resolve().parent / "config.json"
            if _cfg_path.exists():
                _cfg = json.loads(_cfg_path.read_text(encoding="utf-8"))
                _live_cfg = _cfg.get("live") or {}
                # Break length → SEGMENT_TIME (seconds)
                bl_min = int(_live_cfg.get("break_length_min") or 0)
                if bl_min > 0:
                    os.environ["STRMNTR_SEGMENT_TIME"] = str(bl_min * 60)
                # Poll interval — StreaMonitor has no direct env, but
                # we'll apply to WEB_STATUS_FREQUENCY as a hint.
                pi = int(_live_cfg.get("poll_interval_s") or 0)
                if pi > 0:
                    os.environ["STRMNTR_STATUS_FREQ"] = str(pi)
                # Min download speed → FFMPEG_READRATE (bytes/s); skip
                # if 0 so StreaMonitor uses its default.
                ms = int(_live_cfg.get("min_speed_kbps") or 0)
                if ms > 0:
                    os.environ["STRMNTR_FFMPEG_READRATE"] = str(ms * 1024)
        except Exception as _e:
            log.debug(f"[live] apply live settings: {_e}")
        from streamonitor.bot import Bot as _Bot, RoomIdBot as _RoomIdBot   # noqa
        from streamonitor.enums.status import Status as _Status             # noqa
        Bot = _Bot
        RoomIdBot = _RoomIdBot
        Status = _Status

        # Import all site classes by walking the package.
        import pkgutil
        import importlib
        import streamonitor.sites as _sites_pkg
        for mod_info in pkgutil.iter_modules(_sites_pkg.__path__):
            try:
                mod = importlib.import_module(f"streamonitor.sites.{mod_info.name}")
            except Exception as e:
                log.debug(f"  [live] skip site {mod_info.name}: {e}")
                continue
            # Every site module defines exactly one Bot subclass with
            # class attribute `site` (str).
            for attr in dir(mod):
                obj = getattr(mod, attr)
                try:
                    if (isinstance(obj, type) and issubclass(obj, Bot)
                            and obj is not Bot and obj is not RoomIdBot
                            and getattr(obj, "site", None)):
                        SITES[obj.site] = obj
                except Exception:
                    pass
        available = True
        log.info(f"  [live] StreaMonitor found at {_STREAMONITOR_PATH} "
                 f"— {len(SITES)} site modules loaded")
    except Exception as e:
        import_error = f"{type(e).__name__}: {e}"
        log.warning(f"  [live] StreaMonitor import failed ({import_error}); "
                    f"live features disabled")
else:
    import_error = "StreaMonitor not found at any candidate path"
    log.info(f"  [live] {import_error}. Set HARVESTR_STREAMONITOR env var "
             f"or place StreaMonitor at C:\\F\\StreaMonitor.")


# ── Status mapping (StreaMonitor Status enum → UI-friendly strings) ──────────

# Human-readable + UI-color for the status pill. These mirror the semantics
# used in StreaMonitor's own truck-kun skin but with a cleaner palette.
STATUS_UI: Dict[str, Tuple[str, str]] = {
    "UNKNOWN":      ("unknown",    "text-3"),
    "NOTRUNNING":   ("stopped",    "text-3"),
    "ERROR":        ("error",      "bad"),
    "RESTRICTED":   ("restricted", "warn"),
    "ONLINE":       ("connecting", "accent"),
    "PUBLIC":       ("recording",  "good"),
    "NOTEXIST":     ("not found",  "bad"),
    "PRIVATE":      ("private",    "purple"),
    "OFFLINE":      ("offline",    "text-3"),
    "LONG_OFFLINE": ("long offline", "text-3"),
    "DELETED":      ("deleted",    "bad"),
    "RATELIMIT":    ("rate-limited", "warn"),
    "CLOUDFLARE":   ("cloudflare", "warn"),
}


def status_ui(status_name: str) -> Tuple[str, str]:
    return STATUS_UI.get(status_name, (status_name.lower(), "text-3"))


# ── Camsmut downloader sync ──────────────────────────────────────────────────
# When a user is added to live recording, also push them to the front of the
# sibling camsmut downloader's performers list (so they get downloaded first
# next time the camsmut batch runs). Best-effort: silently skipped if the
# camsmut config can't be located, and never propagates exceptions.

_CAMSMUT_CONFIG_DEFAULT = _HERE.parent / "camsmut" / "camsmut_config.json"


def _camsmut_config_path() -> Optional[Path]:
    """Locate the camsmut downloader's config file. Env var wins."""
    override = os.environ.get("HARVESTR_CAMSMUT_CONFIG", "").strip()
    if override:
        p = Path(override)
        return p if p.exists() else None
    return _CAMSMUT_CONFIG_DEFAULT if _CAMSMUT_CONFIG_DEFAULT.exists() else None


def _sync_to_camsmut(usernames) -> None:
    """Push usernames to the front of camsmut's `performers` list.

    Semantics:
      - Case-insensitive dedupe — if a username already exists, it is moved
        to the front (promoted) using its first-seen casing.
      - Atomic write via .json.tmp + os.replace.
      - Multiple usernames preserve their input order: input [A, B, C]
        ends up as [A, B, C, ...rest] at the front of the list.
      - Best-effort: any failure is logged at debug level, never raised.

    Accepts a single string or an iterable of strings.
    """
    if isinstance(usernames, str):
        usernames = [usernames]
    usernames = [u.strip() for u in (usernames or []) if u and u.strip()]
    if not usernames:
        return

    cfg_path = _camsmut_config_path()
    if cfg_path is None:
        log.debug("[live] camsmut sync: config not found (skipping)")
        return

    try:
        with open(cfg_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.debug(f"[live] camsmut sync: load failed: {e}")
        return

    performers = data.get("performers")
    if not isinstance(performers, list):
        performers = []
    # Coerce any stray non-strings out — defensive, the file is hand-edited
    performers = [p for p in performers if isinstance(p, str)]

    added: List[str] = []
    promoted: List[str] = []
    # Iterate in reverse so each insert-at-0 lands the FIRST input at index 0:
    # input [A,B,C] → reverse to C,B,A → insert each at 0 → list ends [A,B,C,...]
    for u in reversed(usernames):
        ul = u.lower()
        old_idx = next((i for i, p in enumerate(performers) if p.lower() == ul), None)
        if old_idx is None:
            performers.insert(0, u)
            added.append(u)
        else:
            if old_idx == 0:
                continue  # already at front — nothing to do
            existing = performers.pop(old_idx)
            performers.insert(0, existing)   # preserve original casing
            promoted.append(existing)

    if not added and not promoted:
        return

    data["performers"] = performers
    tmp = cfg_path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, cfg_path)
    except Exception as e:
        log.debug(f"[live] camsmut sync: write failed: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return

    if added:
        log.info(f"  [live] camsmut sync: queued {added} at front")
    if promoted:
        log.info(f"  [live] camsmut sync: promoted {promoted} to front")


# ── LiveManager — glue layer for the UI ──────────────────────────────────────

@dataclass
class _RunningModel:
    """Thread-safe wrapper around a StreaMonitor Bot instance plus its thread."""
    bot: Any                # streamonitor.bot.Bot
    site: str
    username: str
    room_id: Optional[str] = None
    created_at: str = ""


class LiveManager:
    """Single global coordinator for all running Bots.

    The web UI calls into this with plain strings / dicts; we translate to
    Bot API calls. All methods are thread-safe and fail-gracefully when
    StreaMonitor isn't available.
    """

    def __init__(self, downloads_dir: Path) -> None:
        self.downloads_dir = Path(downloads_dir)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.downloads_dir / "live_models.json"
        self._lock = threading.RLock()
        self._models: Dict[str, _RunningModel] = {}   # key = "username|site"
        # Live recordings folder — honors config.live.live_output_dir if set,
        # otherwise defaults to downloads/_live/.
        self.live_dir = self._resolve_live_dir()
        # On startup, reconstruct from config (do NOT auto-start — user clicks).
        # Defer each bot's synchronous folder scan (cache_file_list) out of
        # __init__ so 1000+ disk scans don't block boot; the background sweeper
        # started below fills in per-model recorded sizes within seconds. Reset
        # the flag right after restore so UI-created bots (one cheap scan) still
        # populate their size immediately.
        if Bot is not None:
            try:
                Bot.defer_init_scan = True
                Bot.suppress_boot_poll = True
            except Exception:
                pass
        try:
            self._restore()
        finally:
            if Bot is not None:
                try:
                    Bot.defer_init_scan = False
                    Bot.suppress_boot_poll = False
                except Exception:
                    pass
        # Spawn the bulk-status poller so bulk-update sites (Chaturbate,
        # CamSoda, StripChat) get ongoing status checks. Without this,
        # bulk-update bots only ever do a single getStatus() at startup
        # (when sc==NOTRUNNING) and then never recheck — so models that
        # weren't online at the exact moment of startup never get
        # detected even when they go live later.
        # Recording count was permanently stuck at whatever subset
        # happened to be PUBLIC during the one-shot poll. (StreaMonitor's
        # native CLI starts BulkStatusManager from main.py; LiveManager
        # never adopted that piece, so we add a thin shim here.)
        self._bulk_poller = self._start_bulk_poller()
        # One-shot background sweep to run the folder scans deferred during
        # _restore() above, without blocking boot.
        self._scan_sweeper = self._start_scan_sweeper()
        # Optional Mullvad VPN auto-rotation: rotate the exit IP on a rate-limit
        # storm, then wake the affected bots. No-op unless configured
        # (vpn_config.json / STRMNTR_VPN_ROTATE) -- see VPN_SETUP.md.
        self._vpn_watchdog = self._start_vpn_watchdog()

    def _start_scan_sweeper(self):
        """One-shot daemon that runs each bot's deferred folder scan
        (cache_file_list) after boot, throttled so 1000+ disk scans don't spike
        CPU/disk at startup. A model's recorded size shows 0 until the sweep
        reaches it (a few seconds); new recordings still update size via the
        bot's own post-recording scan, which sets _video_files_scanned so the
        sweep skips that bot (no double scan). Tunable via
        HARVESTR_SCAN_SWEEP_DELAY (seconds between bots; default 0.05)."""
        if not available or Bot is None:
            return None
        import time as _time
        try:
            delay = float(os.environ.get("HARVESTR_SCAN_SWEEP_DELAY", "0.05"))
        except Exception:
            delay = 0.05

        def _loop() -> None:
            # One-shot snapshot: _restore() ran synchronously before this thread
            # was started, so every restored bot is already in _models. Bots
            # added later via the UI scan synchronously in __init__ (the defer
            # flag is already reset), so a single pass covers everything. Snap
            # under the lock, then scan OUTSIDE it.
            with self._lock:
                bots = [rm.bot for rm in self._models.values()]
            scanned = 0
            for bot in bots:
                if getattr(bot, "_video_files_scanned", False):
                    continue
                try:
                    bot.cache_file_list()
                    scanned += 1
                except Exception as e:
                    log.debug(f"[live] scan sweep {getattr(bot, 'username', '?')}: "
                              f"{type(e).__name__}: {e}")
                if delay > 0:
                    _time.sleep(delay)
            log.info(f"[live] startup folder-scan sweep done "
                     f"({scanned} scanned of {len(bots)})")

        t = threading.Thread(target=_loop, name="live-scan-sweeper",
                             daemon=True)
        t.start()
        return t

    def _start_vpn_watchdog(self):
        """Watch for a rate-limit storm (a flagged exit IP) and rotate the
        Mullvad location, then wake the affected site's bots so they retry on the
        fresh IP. No-op unless rotation is configured (vpn_config.json / env)."""
        try:
            from streamonitor.utils import vpn_rotator as _vpn
        except Exception:
            return None
        if not _vpn.configured():
            log.info("[live] VPN auto-rotation: not configured (no-op)")
            return None
        import time as _time
        try:
            cfg_locs = _vpn._load_cfg().get("rotate_locations", [])
        except Exception:
            cfg_locs = []
        log.info(f"[live] VPN auto-rotation armed: locations={cfg_locs}")

        def _loop() -> None:
            while True:
                _time.sleep(15)
                try:
                    for slug in ("CB", "SC", "CS"):
                        if _vpn.should_rotate(slug):
                            # TIER 2: the same-IP restart didn't help -> rotate.
                            loc = _vpn.rotate(reason=f"{slug} rate-limited",
                                              log=lambda m: log.warning(m))
                            if loc:
                                self._wake_site_bots(slug)
                            break  # at most one rotation per cycle
                        elif _vpn.should_restart(slug):
                            # TIER 1: restart (wake) the bots on the SAME IP first
                            # -- cheap, no VPN disruption. Only escalate to a
                            # rotation if the rate-limits keep climbing after this.
                            _vpn.mark_restart(slug)
                            log.warning(f"[live] [{slug}] rate-limited -> restarting bots "
                                        f"(same IP) before any VPN rotation")
                            self._wake_site_bots(slug)
                except Exception as e:
                    log.debug(f"[live] vpn watchdog: {type(e).__name__}: {e}")

        t = threading.Thread(target=_loop, name="live-vpn-watchdog", daemon=True)
        t.start()
        return t

    def _wake_site_bots(self, site_slug: str) -> None:
        """After a VPN rotation, clear the ratelimit backoff and wake the given
        site's bots so they immediately re-poll on the new exit IP."""
        try:
            with self._lock:
                bots = [rm.bot for rm in self._models.values()
                        if getattr(rm.bot, "siteslug", "") == site_slug]
            for bot in bots:
                try:
                    bot.ratelimit = False
                    bot._offline_time = 0
                    ev = getattr(bot, "_wake_event", None)
                    if ev is not None:
                        ev.set()
                except Exception:
                    pass
            log.info(f"[live] woke {len(bots)} [{site_slug}] bots after VPN rotation")
        except Exception as e:
            log.debug(f"[live] wake bots: {e}")

    def _start_bulk_poller(self):
        """Start a daemon thread that calls each bulk-capable site's
        `getStatusBulk(streamers)` classmethod every 10s, refreshing
        every running bulk-update bot's `sc` from a single API call
        per site instead of one per bot. Mirrors StreaMonitor's
        BulkStatusManager but pulls live state from `self._models`
        each tick so bots added/removed via the UI are picked up
        without needing to restart the poller."""
        if not available or Bot is None:
            return None
        import time as _time
        try:
            from streamonitor.bot import LOADED_SITES as _LOADED_SITES
        except Exception as e:
            log.warning(f"[live] bulk poller: cannot import LOADED_SITES: {e}")
            return None

        bulk_classes = frozenset(
            cls for cls in _LOADED_SITES
            if hasattr(cls, "getStatusBulk")
            and getattr(cls, "bulk_update", False)
        )
        if not bulk_classes:
            log.info("[live] bulk poller: no bulk-update sites loaded")
            return None

        def _loop() -> None:
            log.info(f"[live] bulk poller started for: "
                     f"{sorted(getattr(c, 'site', '?') for c in bulk_classes)}")
            while True:
                try:
                    # Snapshot the running bots per bulk class
                    by_class: Dict[type, set] = {}
                    with self._lock:
                        for rm in self._models.values():
                            bot = rm.bot
                            cls = bot.__class__
                            if cls not in bulk_classes:
                                continue
                            if not getattr(bot, "running", False):
                                continue
                            by_class.setdefault(cls, set()).add(bot)
                    # Poll each class's bulk endpoint
                    for cls, bots in by_class.items():
                        try:
                            cls.getStatusBulk(bots)
                        except Exception as e:
                            log.debug(f"[live] bulk poll {getattr(cls, 'site', cls.__name__)}: "
                                      f"{type(e).__name__}: {e}")
                except Exception as e:
                    log.debug(f"[live] bulk poller iter: {type(e).__name__}: {e}")
                _time.sleep(10)

        t = threading.Thread(target=_loop, name="live-bulk-poller",
                             daemon=True)
        t.start()
        return t

    def _resolve_live_dir(self) -> Path:
        """Live recordings go to config.live.live_output_dir (if set) or
        downloads/_live/. Called at init time — same time the env var for
        StreaMonitor is set, so it's consistent with where recordings land.

        If the user-configured dir is unreachable (e.g. an external drive
        that isn't mounted), fall back to the default. This must match the
        fallback logic in the module-level STRMNTR_DOWNLOAD_DIR setup so
        the Bot threads write somewhere they can actually create folders."""
        default = self.downloads_dir / "_live"
        try:
            cfg_path = Path(__file__).resolve().parent / "config.json"
            if cfg_path.exists():
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                live_dir = (cfg.get("live") or {}).get("live_output_dir") or ""
                if live_dir:
                    p = Path(live_dir).expanduser()
                    try:
                        p.mkdir(parents=True, exist_ok=True)
                        return p
                    except (OSError, FileNotFoundError) as e:
                        log.warning(
                            f"[live] live_output_dir {p} is unreachable "
                            f"({e}); falling back to {default}"
                        )
        except Exception as e:
            log.debug(f"[live] resolve live_dir: {e}")
        default.mkdir(parents=True, exist_ok=True)
        return default

    def model_folder(self, username: str, site: str) -> Path:
        """Where this model's recordings live on disk."""
        # StreaMonitor's output layout: <live_dir>/<username> [SITESLUG]/
        site_cls = SITES.get(site)
        slug = getattr(site_cls, "siteslug", site) if site_cls else site
        return self.live_dir / f"{username} [{slug}]"

    # ── Repair progress state (background thread + UI polling) ─────
    # One repair job at a time. Keyed only by "scope" (one model vs sweep).
    _repair_state: Dict[str, Any] = {
        "active": False,
        "scope": "",           # "model:user|site" or "all"
        "stage": "idle",       # idle | listing | repairing | finished | error
        "current": 0,
        "total": 0,
        "current_file": "",
        "started_at": "",
        "finished_at": "",
        "counts": {"ok": 0, "remuxed": 0, "reencoded": 0, "deleted": 0, "failed": 0},
        "last_result": None,   # most recent RepairResult as dict
        "results": [],         # full list, populated at end
        "folder": "",
        "delete_if_unfixable": False,
    }
    _repair_lock = threading.Lock()

    @classmethod
    def repair_progress(cls) -> Dict[str, Any]:
        """Snapshot of current repair state for the UI to poll."""
        with cls._repair_lock:
            return json.loads(json.dumps(cls._repair_state))  # deep copy

    def _repair_progress_cb(self, stage: str, cur: int, total: int,
                              path: str, partial):
        """Passed to video_repair.sweep_folder. Updates the class-level
        shared state on each progress event."""
        from video_repair import RepairResult
        with self._repair_lock:
            s = self._repair_state
            s["stage"] = stage
            s["current"] = cur
            s["total"] = total
            if path:
                s["current_file"] = os.path.basename(path)
            if partial and isinstance(partial, RepairResult):
                s["counts"][partial.action] = s["counts"].get(partial.action, 0) + 1
                s["last_result"] = {
                    "path": partial.path,
                    "action": partial.action,
                    "reason": partial.reason,
                    "duration_s": partial.duration_s,
                    "before_size": partial.before_size,
                    "after_size": partial.after_size,
                    "elapsed_s": partial.elapsed_s,
                }

    def _run_repair_job(self, *, folder: Path, scope: str,
                         delete_if_unfixable: bool,
                         only_recent_hours: float = 0.0) -> None:
        """Runs inside a background thread. Writes into _repair_state so
        the UI can poll /api/live/repair/status."""
        import video_repair
        now_iso = lambda: __import__("datetime").datetime.now().replace(
            microsecond=0).isoformat()
        with self._repair_lock:
            self._repair_state.update({
                "active": True, "scope": scope, "stage": "starting",
                "current": 0, "total": 0, "current_file": "",
                "started_at": now_iso(), "finished_at": "",
                "counts": {"ok": 0, "remuxed": 0, "reencoded": 0,
                            "deleted": 0, "failed": 0},
                "last_result": None, "results": [],
                "folder": str(folder),
                "delete_if_unfixable": bool(delete_if_unfixable),
            })
        try:
            results = video_repair.sweep_folder(
                str(folder), recursive=True,
                delete_if_unfixable=delete_if_unfixable,
                only_recent_seconds=only_recent_hours * 3600 if only_recent_hours else 0,
                skip_if_locked=True, log=log,
                progress_cb=self._repair_progress_cb,
            )
            with self._repair_lock:
                self._repair_state["results"] = [
                    {
                        "path": r.path, "action": r.action, "reason": r.reason,
                        "duration_s": r.duration_s,
                        "before_size": r.before_size, "after_size": r.after_size,
                        "elapsed_s": r.elapsed_s,
                    } for r in results
                ]
                self._repair_state["stage"] = "finished"
                self._repair_state["finished_at"] = now_iso()
                self._repair_state["active"] = False
        except Exception as e:
            log.error(f"[live] repair job crashed: {e}")
            with self._repair_lock:
                self._repair_state["stage"] = "error"
                self._repair_state["current_file"] = f"error: {e}"
                self._repair_state["finished_at"] = now_iso()
                self._repair_state["active"] = False

    def repair_model(self, username: str, site: str, *,
                      delete_if_unfixable: bool = False) -> Dict[str, Any]:
        """Kick off a background repair of this model's folder.
        Returns immediately with a status handle — poll /api/live/repair/status
        for progress."""
        with self._repair_lock:
            if self._repair_state["active"]:
                return {"error": "another repair job is running",
                        "scope": self._repair_state["scope"]}
        folder = self.model_folder(username, site)
        if not folder.exists():
            return {"error": f"no folder at {folder}",
                    "username": username, "site": site}
        scope = f"model:{username}|{site}"
        t = threading.Thread(
            target=self._run_repair_job,
            kwargs={"folder": folder, "scope": scope,
                     "delete_if_unfixable": delete_if_unfixable},
            daemon=True, name=f"repair-{username}",
        )
        t.start()
        return {"ok": True, "scope": scope, "folder": str(folder), "started": True}

    def repair_all(self, *, delete_if_unfixable: bool = False,
                    only_recent_hours: float = 0.0) -> Dict[str, Any]:
        """Kick off a background sweep of the whole live directory."""
        with self._repair_lock:
            if self._repair_state["active"]:
                return {"error": "another repair job is running",
                        "scope": self._repair_state["scope"]}
        t = threading.Thread(
            target=self._run_repair_job,
            kwargs={"folder": self.live_dir, "scope": "all",
                     "delete_if_unfixable": delete_if_unfixable,
                     "only_recent_hours": only_recent_hours},
            daemon=True, name="repair-all",
        )
        t.start()
        return {"ok": True, "scope": "all", "folder": str(self.live_dir),
                "started": True}

    @staticmethod
    def key_of(username: str, site: str) -> str:
        return f"{username.strip().lower()}|{site.strip()}"

    def _restore(self) -> None:
        """Read the saved model list. Does NOT start any bots."""
        if not self.config_path.exists():
            return
        try:
            entries = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"  [live] config read: {e}")
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            username = (entry.get("username") or "").strip()
            site = (entry.get("site") or "").strip()
            if not username or not site:
                continue
            # We create the Bot instance but don't start its thread unless
            # the saved entry says running=True
            was_running = bool(entry.get("running", False))
            room_id = entry.get("room_id")
            try:
                self._create_bot(username, site, room_id=room_id,
                                  autostart=was_running, _save=False)
            except Exception as e:
                log.warning(f"  [live] restore {username} [{site}]: {e}")
        log.info(f"  [live] restored {len(self._models)} models from config")

    def _save(self) -> None:
        """Persist current model list atomically."""
        entries = []
        with self._lock:
            for _, rm in self._models.items():
                bot = rm.bot
                e: Dict[str, Any] = {
                    "username": rm.username,
                    "site": rm.site,
                    "running": bool(getattr(bot, "running", False)),
                }
                if rm.room_id:
                    e["room_id"] = rm.room_id
                entries.append(e)
        tmp = self.config_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(entries, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, self.config_path)
        except Exception as e:
            log.warning(f"  [live] save: {e}")

    def _create_bot(self, username: str, site: str,
                    *, room_id: Optional[str] = None,
                    autostart: bool = False, _save: bool = True) -> Any:
        if not available:
            raise RuntimeError("StreaMonitor not available. "
                               "Set HARVESTR_STREAMONITOR env var or install "
                               "StreaMonitor at C:\\F\\StreaMonitor.")
        site_cls = SITES.get(site)
        if site_cls is None:
            raise ValueError(f"unsupported site {site!r}; supported: "
                             f"{sorted(SITES.keys())}")
        key = self.key_of(username, site)
        with self._lock:
            if key in self._models:
                return self._models[key].bot
            # RoomIdBot subclasses take an extra room_id arg
            try:
                if RoomIdBot and issubclass(site_cls, RoomIdBot):
                    bot = site_cls(username, room_id=room_id)
                else:
                    bot = site_cls(username)
            except TypeError:
                # Older site modules may not accept room_id kw; fall back
                bot = site_cls(username)
            rm = _RunningModel(
                bot=bot, site=site, username=username,
                room_id=room_id,
                created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            )
            self._models[key] = rm
            if autostart:
                try:
                    bot.restart()   # StreaMonitor's entry — sets running=True, starts thread
                except Exception as e:
                    log.warning(f"  [live] autostart {key}: {e}")
        if _save:
            self._save()
        return rm.bot

    # ── Public API ───────────────────────────────────────────────────────

    def list_sites(self) -> List[Dict[str, Any]]:
        if not available:
            return []
        out = []
        for name, cls in sorted(SITES.items()):
            out.append({
                "name": name,
                "slug": getattr(cls, "siteslug", ""),
                "needs_room_id": bool(RoomIdBot and issubclass(cls, RoomIdBot)),
                "bulk": bool(getattr(cls, "bulk_update", False)),
            })
        return out

    def bulk_add(self, entries: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Add many models at once. `entries` is a list of dicts with
        {username, site, room_id?} — same schema as StreaMonitor's config.json.
        Duplicates are silently skipped. Returns counts."""
        added = 0
        errors: List[str] = []
        synced_users: List[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            u = (entry.get("username") or "").strip()
            s = (entry.get("site") or "").strip()
            rid = entry.get("room_id")
            if not u or not s:
                continue
            try:
                # Suppress per-call camsmut sync; we batch-sync at the end
                # to do a single atomic write and preserve input order.
                self.add_model(u, s, room_id=rid, _sync_camsmut=False)
                added += 1
                synced_users.append(u)
            except Exception as e:
                errors.append(f"{u}|{s}: {e}")
        if synced_users:
            _sync_to_camsmut(synced_users)
        return {"ok": True, "added": added, "errors": errors,
                "total": len(self._models)}

    def add_model(self, username: str, site: str,
                  room_id: Optional[str] = None,
                  _sync_camsmut: bool = True,
                  autostart: bool = True) -> Dict[str, Any]:
        username = (username or "").strip()
        site = (site or "").strip()
        if not username:
            raise ValueError("username required")
        # autostart=True by default: adding a performer "to download" should
        # immediately start tracking it (poll status + record when online).
        # Previously autostart=False left the bot created-but-idle, so a freshly
        # added model never tracked until the user manually clicked Start -- which
        # is the "loads but doesn't start tracking after add" report. The UI
        # already shows an optimistic running/"starting…" card, so this aligns
        # the backend with what the user sees.
        self._create_bot(username, site, room_id=room_id, autostart=autostart)
        # Mirror this user into the camsmut downloader's performers list
        # (front of queue). Suppressed by bulk_add for batched syncing.
        if _sync_camsmut:
            _sync_to_camsmut(username)
        return {"ok": True, "key": self.key_of(username, site)}

    def remove_model(self, username: str, site: str) -> Dict[str, Any]:
        key = self.key_of(username, site)
        with self._lock:
            rm = self._models.pop(key, None)
        if rm:
            try:
                if getattr(rm.bot, "running", False):
                    rm.bot.stop(thread_too=True)
            except Exception as e:
                log.debug(f"  [live] remove {key}: {e}")
        self._save()
        return {"ok": True, "removed": bool(rm)}

    def start_model(self, username: str, site: str) -> Dict[str, Any]:
        key = self.key_of(username, site)
        with self._lock:
            rm = self._models.get(key)
            if not rm:
                raise LookupError(f"no such model {key}")
            bot = rm.bot
            # Fresh-instantiate if the previous thread has already exited —
            # Thread objects in Python can only be started once.
            if not bot.is_alive() and getattr(bot, "running", False) is False:
                site_cls = SITES.get(rm.site)
                if site_cls:
                    try:
                        if RoomIdBot and issubclass(site_cls, RoomIdBot):
                            bot = site_cls(rm.username, room_id=rm.room_id)
                        else:
                            bot = site_cls(rm.username)
                        rm.bot = bot
                    except Exception as e:
                        log.debug(f"  [live] re-instantiate {key}: {e}")
            try:
                bot.restart()    # StreaMonitor convention: sets self.running=True,
                                 # spawns or resumes thread
            except Exception as e:
                log.warning(f"  [live] start {key}: {e}")
        self._save()
        return {"ok": True}

    def stop_model(self, username: str, site: str) -> Dict[str, Any]:
        key = self.key_of(username, site)
        with self._lock:
            rm = self._models.get(key)
            if not rm:
                raise LookupError(f"no such model {key}")
            try:
                rm.bot.stop(thread_too=False)
            except Exception as e:
                log.debug(f"  [live] stop {key}: {e}")
        self._save()
        return {"ok": True}

    def toggle_all(self, running: bool) -> Dict[str, Any]:
        n = 0
        for key in list(self._models.keys()):
            try:
                user, site = key.split("|", 1)
                (self.start_model if running else self.stop_model)(user, site)
                n += 1
            except Exception as e:
                log.debug(f"  [live] bulk toggle {key}: {e}")
        return {"ok": True, "count": n}

    def get_snapshot(self) -> Dict[str, Any]:
        """Build the full UI-facing state snapshot for the Live tab."""
        models: List[Dict[str, Any]] = []
        recording_count = 0
        total_sessions_bytes = 0
        status_hist: Dict[str, int] = {}

        # Lazy-init the history tracker (file-backed)
        if getattr(self, "_history", None) is None:
            try:
                from live_history import LiveHistory
                self._history = LiveHistory(self.downloads_dir)
            except Exception as e:
                log.debug(f"[live] history init: {e}")
                self._history = None

        # Snapshot the model list under the lock, then build the per-model dicts
        # OUTSIDE it. This loop does metadata extraction, history file-I/O and
        # freq computation for 1000+ models; holding self._lock across all of it
        # serialized every other lock user and starved the fast summary endpoint
        # to 45s timeouts under browser polling. LiveHistory has its own lock and
        # bot attributes are read live -- an eventually-consistent UI snapshot.
        with self._lock:
            items = sorted(self._models.items(),
                           key=lambda kv: (kv[1].site, kv[1].username.lower()))
        for _, rm in items:
            bot = rm.bot
            status_name = getattr(getattr(bot, "sc", None), "name", "UNKNOWN")
            status_hist[status_name] = status_hist.get(status_name, 0) + 1
            label, color = status_ui(status_name)
            is_running = bool(getattr(bot, "running", False))
            is_recording = bool(getattr(bot, "recording", False))
            if is_recording:
                recording_count += 1
            # Total file size for this model (StreaMonitor caches in
            # video_files_total_size on the Bot)
            size_bytes = int(getattr(bot, "video_files_total_size", 0) or 0)
            total_sessions_bytes += size_bytes

            # Extract rich metadata from bot.lastInfo (StripChat etc.
            # expose age, country, language, tags, stream_duration,
            # follower/spectator count, avatar/thumbnail URLs, etc.)
            last_info = getattr(bot, "lastInfo", {}) or {}
            enriched = _extract_rich_meta(last_info)

            # Record state transition in history ledger (transition-only)
            key = self.key_of(rm.username, rm.site)
            if self._history:
                try:
                    self._history.record(key, status_name, meta=enriched)
                except Exception as e:
                    log.debug(f"[live] record {key}: {e}")

            # Derived freq metrics
            freq = self._history.snapshot(key) if self._history else {}

            models.append({
                "key": key,
                "username": rm.username,
                "site": rm.site,
                "site_slug": getattr(bot, "siteslug", ""),
                "room_id": rm.room_id or "",
                "running": is_running,
                "recording": is_recording,
                "status": status_name,
                "status_label": label,
                "status_color": color,
                "size_bytes": size_bytes,
                "gender": getattr(getattr(bot, "gender", None), "value", "") or enriched.get("gender", ""),
                "country": getattr(bot, "country", "") or enriched.get("country", ""),
                "language": enriched.get("language", ""),
                "age": enriched.get("age"),
                "tags": enriched.get("tags", []),
                "avatar_url": enriched.get("avatar_url", ""),
                "thumb_url": enriched.get("thumb_url", ""),
                "spectators": enriched.get("spectators"),
                "followers": enriched.get("followers"),
                "stream_duration_s": enriched.get("stream_duration_s"),
                # Derived frequency metrics (from LiveHistory)
                "last_online_ts": freq.get("last_online_ts", ""),
                "last_offline_ts": freq.get("last_offline_ts", ""),
                "online_sessions_7d": freq.get("online_sessions_7d", 0),
                "online_hours_7d": freq.get("online_hours_7d", 0),
                "avg_session_minutes": freq.get("avg_session_minutes", 0),
                "next_predicted_ts": freq.get("next_predicted_ts", ""),
                "peak_hour_utc": freq.get("peak_hour_utc", -1),
            })

        return {
            "available": available,
            "import_error": import_error,
            "streamonitor_path": _STREAMONITOR_PATH or "",
            "summary": {
                "total": len(models),
                "running": sum(1 for m in models if m["running"]),
                "recording": recording_count,
                "total_bytes": total_sessions_bytes,
                "status_hist": status_hist,
                **self._disk_summary(),
            },
            "models": models,
        }

    def _disk_summary(self) -> Dict[str, Any]:
        """Free/total bytes on the recordings drive — for the UI disk gauge."""
        try:
            import shutil
            du = shutil.disk_usage(str(self.live_dir))
            return {
                "disk_free_bytes": du.free,
                "disk_total_bytes": du.total,
                "disk_used_pct": round((du.used / du.total) * 100, 1) if du.total else 0,
            }
        except Exception:
            return {"disk_free_bytes": None, "disk_total_bytes": None,
                    "disk_used_pct": None}

    def live_summary(self) -> Dict[str, Any]:
        """Cheap header stats (counts + bytes + disk + status histogram) without
        the per-model metadata/history work get_snapshot() does.

        Cached ~1.5 s AND computed OUTSIDE the models lock (we hold it only to
        snapshot the bot list), so a lock held long by get_snapshot during the
        startup CPU crunch or heavy polling can't stall this fast endpoint —
        which is what made it time out at scale."""
        import time as _t
        now = _t.monotonic()
        cache = getattr(self, "_summary_cache", None)
        if cache is None:
            cache = self._summary_cache = {"ts": 0.0, "data": None}
        if cache["data"] is not None and (now - cache["ts"]) < 1.5:
            return cache["data"]
        # Brief lock: just grab the bot references, then tally without it.
        with self._lock:
            bots = [rm.bot for rm in self._models.values()]
        running = recording = 0
        total_bytes = 0
        status_hist: Dict[str, int] = {}
        for bot in bots:
            if getattr(bot, "running", False):
                running += 1
            if getattr(bot, "recording", False):
                recording += 1
            total_bytes += int(getattr(bot, "video_files_total_size", 0) or 0)
            name = getattr(getattr(bot, "sc", None), "name", "UNKNOWN")
            status_hist[name] = status_hist.get(name, 0) + 1
        out: Dict[str, Any] = {
            "total": len(bots), "running": running, "recording": recording,
            "total_bytes": total_bytes, "status_hist": status_hist,
        }
        out.update(self._disk_summary())
        cache["data"] = out
        cache["ts"] = now
        return out

    @staticmethod
    def _scrub_last_info(info: Dict[str, Any]) -> Dict[str, Any]:
        """Strip huge / binary values from bot.lastInfo so it's JSON-safe
        and small enough to transit on every /api/live/status poll."""
        if not isinstance(info, dict):
            return {}
        safe = {}
        for k, v in info.items():
            if isinstance(v, (str, int, float, bool, type(None))):
                if isinstance(v, str) and len(v) > 200:
                    v = v[:200] + "..."
                safe[k] = v
            elif isinstance(v, (list, tuple)):
                safe[k] = len(v)
        return safe


# ──────────────────────────────────────────────────────────────────────
def _extract_rich_meta(info: Dict[str, Any]) -> Dict[str, Any]:
    """Pull display-friendly metadata out of the site-specific bot.lastInfo.

    Handles schema variations across StripChat / Chaturbate / CamSoda /
    BongaCams / etc. — each API returns different field names. We try
    common paths for every metric and keep the first non-empty value."""
    if not isinstance(info, dict):
        return {}

    def _first(paths: list) -> Any:
        for p in paths:
            if isinstance(p, str):
                if p in info and info[p] not in (None, ""):
                    return info[p]
                continue
            # Path is a list of keys
            cur = info
            for k in p:
                if isinstance(cur, dict) and k in cur:
                    cur = cur[k]
                else:
                    cur = None
                    break
            if cur not in (None, ""):
                return cur
        return None

    out: Dict[str, Any] = {}

    # Country — StripChat: country, geo.country, location.country
    country = _first(["country",
                       ["geo", "country"],
                       ["location", "country"],
                       "countryCode",
                       ["user", "country"]])
    if country:
        out["country"] = str(country).upper() if len(str(country)) == 2 else str(country)

    # Language / spoken
    lang = _first(["language",
                    ["broadcastLanguage"],
                    ["user", "language"],
                    "spokenLanguages"])
    if isinstance(lang, list) and lang:
        lang = lang[0]
    if lang:
        out["language"] = str(lang)

    # Age
    age = _first(["age",
                   ["user", "age"],
                   ["broadcaster", "age"]])
    if isinstance(age, (int, float)) and 18 <= age <= 99:
        out["age"] = int(age)

    # Tags (first 5)
    tags = _first(["tags",
                    ["model", "tags"],
                    ["user", "tags"],
                    "labels",
                    "topics"])
    if isinstance(tags, list):
        clean = []
        for t in tags[:10]:
            if isinstance(t, dict):
                t = t.get("name") or t.get("slug") or ""
            if isinstance(t, str) and t.strip():
                clean.append(t.strip()[:24])
        if clean:
            out["tags"] = clean[:8]

    # Gender (if not already from bot.gender)
    gender = _first(["gender", ["user", "gender"], "genderType"])
    if gender:
        out["gender"] = str(gender)

    # Avatar / thumbnail — large poster OK for card background
    for key_local, paths in (
        ("avatar_url", ["avatarUrl", "avatar", "profilePictureUrl",
                         ["user", "avatarUrl"], "imageUrl",
                         ["broadcaster", "avatar"]]),
        ("thumb_url", ["thumbnail", "thumbUrl", "snapshotURL", "previewURL",
                        ["stream", "thumbnail"], "cameraSnapshot"]),
    ):
        val = _first(paths)
        if isinstance(val, str) and val.startswith(("http", "//")):
            out[key_local] = val if val.startswith("http") else "https:" + val

    # Counters
    spec = _first(["viewers", "spectators", "viewersCount",
                    ["stream", "viewers"], ["cam", "viewers"]])
    if isinstance(spec, (int, float)):
        out["spectators"] = int(spec)

    followers = _first(["followers", "followerCount", "subsCount",
                         ["user", "followers"]])
    if isinstance(followers, (int, float)):
        out["followers"] = int(followers)

    # Stream duration (seconds since broadcast started)
    dur = _first(["broadcastDuration", "streamDuration",
                   ["stream", "duration"]])
    if isinstance(dur, (int, float)) and dur >= 0:
        out["stream_duration_s"] = int(dur)

    return out
