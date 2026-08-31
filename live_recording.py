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
from typing import Any, Dict, List, Optional, Set, Tuple

log = logging.getLogger("harvestr.live")


# ── Alerts ring buffer ───────────────────────────────────────────────────────
# Keeps the last WARNING+/ERROR log records for the UI "alerts feed" (the RAG's
# why-as-a-history). Attaches to the ROOT logger so it also captures the vendored
# site/bot/downloader warnings (rotations, model-deleted, stuck models, etc.).
class _RingLogHandler(logging.Handler):
    def __init__(self, maxlen: int = 80):
        super().__init__(level=logging.WARNING)
        from collections import deque
        self.records = deque(maxlen=maxlen)

    def emit(self, record):
        try:
            self.records.append({
                "ts": record.created,
                "level": record.levelname,
                "msg": record.getMessage()[:200],
            })
        except Exception:
            pass


_ALERTS = _RingLogHandler(80)
try:
    logging.getLogger().addHandler(_ALERTS)
except Exception:
    pass


def recent_alerts(limit: int = 40) -> list:
    """Most-recent-first list of the last WARNING+ log records for the UI feed."""
    try:
        return list(_ALERTS.records)[-limit:][::-1]
    except Exception:
        return []


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
    try:
        if _cand and (Path(_cand) / "streamonitor" / "bot.py").exists():
            _STREAMONITOR_PATH = _cand
            break
    except OSError:
        # A candidate on an UNMOUNTED drive (e.g. the D:\F\StreaMonitor fallback
        # when this machine has no D:) makes .exists() raise [WinError 3] instead
        # of returning False, which spammed the log ~1900x. Skip it and continue.
        continue


# Where live recordings go when config.json doesn't name a directory.
# Deliberately the dedicated drive rather than a folder inside the repo:
# recordings are large and continuous, and defaulting them onto the system
# disk is how C: ends up full mid-capture (and how stray captures ended up
# under C:/F/recordings). Override with HARVESTR_LIVE_DEFAULT.
DEFAULT_LIVE_DIR = os.environ.get("HARVESTR_LIVE_DEFAULT") or "E:\\F\\Recordings"

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
        _LIVE_DEFAULT = Path(DEFAULT_LIVE_DIR)
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
        # Create the configured live dir if we can. If its drive isn't mounted
        # we deliberately KEEP pointing at it rather than silently redirecting
        # to the system disk — recordings belong on the drive the user chose,
        # and a library split across two disks is worse than a paused one.
        # bot.py's _recordings_base_ok() holds recording (logging once) while
        # the drive is away and resumes on its own when it returns.
        try:
            _LIVE_DIR.mkdir(parents=True, exist_ok=True)
        except (OSError, FileNotFoundError) as _mk_err:
            log.warning(
                f"  [live] configured live_output_dir {_LIVE_DIR} is currently "
                f"unreachable ({_mk_err}); recording will HOLD until that drive "
                f"is attached (nothing will be written to the system disk)"
            )
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
                # Retention: keep only the N newest recordings per model.
                # This setting existed in the UI and in config.json but
                # nothing ever read it, so it silently did nothing.
                kn = int(_live_cfg.get("keep_last_n") or 0)
                if kn > 0:
                    os.environ["STRMNTR_KEEP_LAST_N"] = str(kn)
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
        # Per-site recording switch. A site in here records nothing: its bots
        # are stopped on toggle-off and are skipped by every start path
        # (restore, add, start-all) until the user turns it back on. Loaded
        # BEFORE _restore() so a disabled site never comes back on a restart.
        self._disabled_sites: Set[str] = self._load_disabled_sites()
        if self._disabled_sites:
            log.info(f"  [live] recording disabled for site(s): "
                     f"{', '.join(sorted(self._disabled_sites))}")
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
                except Exception:
                    pass
        # Keep suppress_boot_poll TRUE for a short window AFTER restore, then
        # reset it in the background, so the bulk poller covers the restored bots
        # BEFORE they could self-poll. Otherwise 1000+ bulk bots fire getStatus
        # in the gap between restore and the first bulk poll -- a burst that
        # flagged the exit IP (seen as 500+ models RATELIMIT right after boot).
        # UI-added bots (created after this resets) still self-poll for a prompt
        # initial status.
        if Bot is not None:
            def _unsuppress():
                import time as _t
                _t.sleep(45)
                try:
                    Bot.suppress_boot_poll = False
                    log.info("[live] bulk self-poll re-enabled (boot window over)")
                except Exception:
                    pass
            threading.Thread(target=_unsuppress, name="live-unsuppress",
                             daemon=True).start()
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
        # Self-clean the rolling-playlist temp dirs (M3U8_TMP) the writer leaves
        # behind when recordings end: sweep ~20s after boot, then every 30 min, so
        # the temp root never accumulates stale dirs across runs. Only removes dirs
        # idle > 2 min, so active recordings are never touched.
        self._tmp_sweeper = self._start_tmp_sweeper()
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

    def _start_tmp_sweeper(self):
        """Background: purge stale rolling-playlist temp dirs (M3U8_TMP) the writer
        leaves behind when recordings end. Sweeps ~20s after boot (clears past-run
        leftovers), then every 30 min. sweep_stale_tmp_dirs() only removes dirs
        idle > 2 min, so active recordings are never touched."""
        def _loop():
            import time as _t
            try:
                from streamonitor.downloaders.hls import sweep_stale_tmp_dirs
            except Exception:
                return
            _t.sleep(20)  # let boot settle before the first sweep
            while True:
                try:
                    sweep_stale_tmp_dirs(120.0, logger=log)
                except Exception:
                    pass
                _t.sleep(1800)  # every 30 min
        t = threading.Thread(target=_loop, name="live-tmp-sweeper", daemon=True)
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
                                # IP changed -> restart IP-bound (StripChat) captures
                                # now so they grab a fresh token instead of stalling.
                                self.restart_ip_bound_recordings()
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

    def _sync_renames(self) -> int:
        """Re-key models whose bot renamed itself mid-run.

        StripChat models can change username; the bot follows the pointer and
        updates bot.username (see StripChat._followRename). The manager keys
        off its OWN copy, so without this the dict key, the UI label and
        live_models.json all keep the dead name — and on the next restart we'd
        re-create the bot under a username that 404s forever.
        """
        renamed: List[Tuple[str, str, str]] = []      # (old_key, new_key, new_user)
        with self._lock:
            for key, rm in list(self._models.items()):
                cur = (getattr(rm.bot, "username", "") or "").strip()
                if not cur or cur == rm.username:
                    continue
                new_key = self.key_of(cur, rm.site)
                if new_key in self._models:
                    # The new name is already tracked separately — drop the
                    # stale duplicate rather than clobbering the live entry.
                    self._models.pop(key, None)
                    log.info(f"[live] {rm.username} -> {cur} [{rm.site}] already "
                             f"tracked; removed the duplicate old entry")
                    renamed.append((key, new_key, cur))
                    continue
                self._models.pop(key, None)
                rm.username = cur
                self._models[new_key] = rm
                renamed.append((key, new_key, cur))
        for old_key, new_key, _ in renamed:
            log.info(f"[live] model renamed: {old_key} -> {new_key}")
        if renamed:
            self._save()
        return len(renamed)

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

    def restart_ip_bound_recordings(self) -> None:
        """After a VPN rotation the exit IP changed, so sites whose stream tokens
        are bound to the IP (StripChat/doppiocdn -> tokens_ip_bound) now hold a
        DEAD token and can't ride through. Stop those active captures so each
        bot's run loop immediately re-fetches a FRESH token on the new IP (a few
        seconds) instead of waiting ~60s for the stall watchdog. Sites that ride
        through (Chaturbate: segments not IP-bound) are deliberately left alone."""
        try:
            with self._lock:
                bots = [rm.bot for rm in self._models.values()
                        if getattr(rm.bot, "tokens_ip_bound", False)
                        and getattr(rm.bot, "recording", False)]
            n = 0
            for bot in bots:
                sd = getattr(bot, "stopDownload", None)
                if callable(sd):
                    try:
                        sd()
                        n += 1
                    except Exception:
                        pass
            if n:
                log.info(f"[live] restarted {n} IP-bound recording(s) for a fresh "
                         "token after VPN rotation")
        except Exception as e:
            log.debug(f"[live] restart ip-bound: {e}")

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
                # Cheap attribute compare over the fleet; piggybacks on this
                # loop so a self-renamed bot is re-keyed within ~10s.
                try:
                    self._sync_renames()
                except Exception as e:
                    log.debug(f"[live] sync renames: {type(e).__name__}: {e}")
                _time.sleep(10)

        t = threading.Thread(target=_loop, name="live-bulk-poller",
                             daemon=True)
        t.start()
        return t

    def _resolve_live_dir(self) -> Path:
        """Live recordings go to config.live.live_output_dir (if set) or
        downloads/_live/. Called at init time — same time the env var for
        StreaMonitor is set, so it's consistent with where recordings land.

        A configured dir is honoured even when its drive is not currently
        mounted: we return it anyway so paths stay stable and recordings keep
        landing on the drive the user picked once it is reattached. Recording
        itself is held by bot.py's availability check meanwhile — we never
        redirect to the system disk behind the user's back."""
        default = Path(DEFAULT_LIVE_DIR)
        try:
            cfg_path = Path(__file__).resolve().parent / "config.json"
            if cfg_path.exists():
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                live_dir = (cfg.get("live") or {}).get("live_output_dir") or ""
                if live_dir:
                    p = Path(live_dir).expanduser()
                    try:
                        p.mkdir(parents=True, exist_ok=True)
                    except (OSError, FileNotFoundError) as e:
                        log.warning(
                            f"[live] live_output_dir {p} is unreachable ({e}); "
                            f"keeping it configured — recording holds until the "
                            f"drive is attached"
                        )
                    return p
        except Exception as e:
            log.debug(f"[live] resolve live_dir: {e}")
        default.mkdir(parents=True, exist_ok=True)
        return default

    # ── Per-site enable/disable ──────────────────────────────────────────
    #
    # Stored in config.json under live.disabled_sites (a list of site names)
    # rather than live_models.json, because it's a user setting about SITES,
    # not about the tracked model list. Storing the DISABLED set (rather than
    # the enabled one) keeps newly-added site modules on by default.

    @property
    def _app_config_path(self) -> Path:
        return Path(__file__).resolve().parent / "config.json"

    def _load_disabled_sites(self) -> Set[str]:
        try:
            p = self._app_config_path
            if p.exists():
                cfg = json.loads(p.read_text(encoding="utf-8"))
                raw = (cfg.get("live") or {}).get("disabled_sites") or []
                return {str(s) for s in raw if str(s).strip()}
        except Exception as e:
            log.debug(f"[live] load disabled_sites: {e}")
        return set()

    def _save_disabled_sites(self) -> None:
        """Merge the disabled set into config.json without clobbering the rest.

        Re-reads the file first: the Settings modal writes the same config, so
        a cached copy would silently revert whatever the user changed there.
        """
        p = self._app_config_path
        try:
            cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
            if not isinstance(cfg, dict):
                cfg = {}
            cfg.setdefault("live", {})["disabled_sites"] = sorted(self._disabled_sites)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, p)
        except Exception as e:
            log.warning(f"  [live] save disabled_sites: {e}")

    def site_enabled(self, site: str) -> bool:
        return site not in self._disabled_sites

    def toggle_site(self, site: str, running: bool) -> Dict[str, Any]:
        """Enable or disable recording for one whole site, live.

        Disabling stops every bot on that site immediately -- Bot.stop() calls
        stopDownload(), which kills the in-flight ffmpeg, so an active capture
        ends within a second or two. No restart of Harvestr is involved, and
        models stay in the list so re-enabling picks them straight back up.
        """
        if not available:
            raise RuntimeError("StreaMonitor not available.")
        site = (site or "").strip()
        if not site:
            raise ValueError("site required")
        if site not in SITES:
            raise ValueError(f"unsupported site {site!r}; supported: "
                             f"{sorted(SITES.keys())}")

        with self._lock:
            if running:
                self._disabled_sites.discard(site)
            else:
                self._disabled_sites.add(site)
            keys = [k for k, rm in self._models.items() if rm.site == site]
        self._save_disabled_sites()

        # Apply to the running fleet. Do this OUTSIDE the manager lock:
        # stop() joins on ffmpeg teardown, and holding _lock across hundreds of
        # bots would stall every dashboard poll (see the lock-light snapshot
        # rule in get_snapshot).
        changed = 0
        was_recording = 0
        for key in keys:
            try:
                user, _ = key.split("|", 1)
                if running:
                    self.start_model(user, site, _save=False)
                else:
                    with self._lock:
                        rm = self._models.get(key)
                    if rm is not None and getattr(rm.bot, "recording", False):
                        was_recording += 1
                    self.stop_model(user, site, _save=False)
                changed += 1
            except Exception as e:
                log.debug(f"  [live] toggle_site {key}: {e}")
        self._save()

        log.info(f"[live] site {site} recording "
                 f"{'ENABLED' if running else 'DISABLED'} "
                 f"({changed} model(s){'' if running else f', {was_recording} mid-recording stopped'})")
        return {"ok": True, "site": site, "enabled": running,
                "models": changed, "stopped_recording": was_recording}

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
            # Never autostart into a site the user has switched off — this is
            # what makes the toggle survive both a restart (_restore) and a
            # later add of a new model on that site.
            if autostart and self.site_enabled(site):
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
                "enabled": self.site_enabled(name),
                "tracked": sum(1 for rm in self._models.values()
                               if rm.site == name),
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

    def start_model(self, username: str, site: str,
                    _save: bool = True) -> Dict[str, Any]:
        key = self.key_of(username, site)
        # A disabled site must stay silent even if the user hits Start on an
        # individual card, otherwise the per-site switch leaks one model at a
        # time and the "site is off" promise stops being true.
        if not self.site_enabled(site):
            return {"ok": False, "skipped": "site_disabled", "site": site}
        # Lock ONLY to read the entry. Constructing a bot hits the network
        # (StripChat resolves a room id and pulls static config), and
        # bot.restart() can join a thread -- doing either under the manager
        # lock serialises the whole fleet behind network latency, because the
        # snapshot builder, bulk poller, rename sync and every start_model call
        # contend for that same lock. Measured effect: "start all" advanced at
        # ~6 models/min, i.e. ~2.5 h for 1554 models.
        with self._lock:
            rm = self._models.get(key)
            if not rm:
                raise LookupError(f"no such model {key}")
            bot = rm.bot
            site_name, username, room_id = rm.site, rm.username, rm.room_id

        # Fresh-instantiate if the previous thread already exited -- a Thread
        # object can only be started once. Done OUTSIDE the lock.
        if not bot.is_alive() and getattr(bot, "running", False) is False:
            site_cls = SITES.get(site_name)
            if site_cls:
                try:
                    if RoomIdBot and issubclass(site_cls, RoomIdBot):
                        new_bot = site_cls(username, room_id=room_id)
                    else:
                        new_bot = site_cls(username)
                    # Re-check under the lock: the entry may have been removed
                    # or replaced while we were building this one.
                    with self._lock:
                        cur = self._models.get(key)
                        if cur is not None and cur.bot is bot:
                            cur.bot = new_bot
                            bot = new_bot
                except Exception as e:
                    log.debug(f"  [live] re-instantiate {key}: {e}")
        try:
            bot.restart()    # StreaMonitor convention: sets self.running=True,
                             # spawns or resumes thread
        except Exception as e:
            log.warning(f"  [live] start {key}: {e}")
        if _save:
            self._save()
        return {"ok": True}

    def stop_model(self, username: str, site: str,
                   _save: bool = True) -> Dict[str, Any]:
        key = self.key_of(username, site)
        with self._lock:
            rm = self._models.get(key)
            if not rm:
                raise LookupError(f"no such model {key}")
            try:
                rm.bot.stop(thread_too=False)
            except Exception as e:
                log.debug(f"  [live] stop {key}: {e}")
        if _save:
            self._save()
        return {"ok": True}

    def toggle_all(self, running: bool) -> Dict[str, Any]:
        n = 0
        skipped = 0
        for key in list(self._models.keys()):
            try:
                user, site = key.split("|", 1)
                # "Start all" respects the per-site switches rather than
                # overriding them — otherwise one click would silently
                # re-enable every site the user had deliberately turned off.
                if running and not self.site_enabled(site):
                    skipped += 1
                    continue
                (self.start_model if running else self.stop_model)(
                    user, site, _save=False)
                n += 1
            except Exception as e:
                log.debug(f"  [live] bulk toggle {key}: {e}")
        self._save()
        out: Dict[str, Any] = {"ok": True, "count": n}
        if skipped:
            out["skipped_disabled_sites"] = skipped
        return out

    def stop_all_for_shutdown(self) -> Dict[str, Any]:
        """Stop every recorder in memory WITHOUT persisting `running: false`.

        The graceful-stop script drains recordings before killing the process
        so ffmpeg closes its files cleanly (exFAT has no journal, and a hard
        kill mid-write corrupted four model folders). But draining via
        toggle_all(False) also SAVED the stopped state, so the next boot
        restored all 1551 models as stopped and recorded nothing until someone
        pressed Start all.

        Stopping without saving keeps the on-disk file describing what the user
        wanted running, so a restart comes back recording on its own.
        """
        with self._lock:
            bots = [rm.bot for rm in self._models.values()]
        stopped = 0
        for bot in bots:
            try:
                if getattr(bot, "running", False):
                    bot.stop(thread_too=False)
                    stopped += 1
            except Exception as e:
                log.debug(f"  [live] shutdown stop: {e}")
        log.info(f"[live] drained {stopped} recorder(s) for shutdown "
                 f"(running-state deliberately NOT persisted)")
        return {"ok": True, "stopped": stopped}

    def get_snapshot(self) -> Dict[str, Any]:
        """Build the full UI-facing state snapshot for the Live tab."""
        models: List[Dict[str, Any]] = []
        recording_count = 0
        total_sessions_bytes = 0
        status_hist: Dict[str, int] = {}
        _active: List = []                        # (path, username, site) for recording bots
        _sites: Dict[str, Dict[str, int]] = {}    # per-site tally for the mini RAG dots

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
            _s = _sites.setdefault(rm.site, {"total": 0, "recording": 0, "public": 0, "error": 0})
            _s["total"] += 1
            if status_name == "PUBLIC":
                _s["public"] += 1
            elif status_name == "ERROR":
                _s["error"] += 1
            if is_recording:
                recording_count += 1
                _s["recording"] += 1
                _out = getattr(bot, "_current_output", None)
                if _out:
                    _active.append((_out, rm.username, rm.site))
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

        summary = {
            "total": len(models),
            "running": sum(1 for m in models if m["running"]),
            "recording": recording_count,
            "total_bytes": total_sessions_bytes,
            "status_hist": status_hist,
            "download_bps": self._active_stats(_active),
            **self._disk_summary(),
        }
        summary["download_bps_avg"] = getattr(self, "_download_bps_avg", 0.0)
        summary["network_bps"] = self._network_stats()
        summary["network_bps_avg"] = getattr(self, "_network_bps_avg", 0.0)
        summary["network_hist"] = list(getattr(self, "_net_hist", []))
        summary["avg_fragment_mb"] = getattr(self, "_avg_fragment_mb", 0.0)
        summary["top_recorders"] = getattr(self, "_top_recorders", [])
        summary["speed_hist"] = list(getattr(self, "_speed_hist", []))
        summary["sites"] = self._site_health(_sites)
        summary["alerts"] = recent_alerts()
        summary["uptime_s"] = self._uptime()
        summary["disk_full_eta_s"] = self._disk_eta(summary.get("disk_free_bytes"))
        summary["health"] = self._compute_health(summary)
        # cache the derived bits so the cheap live_summary can serve them too
        self._health = summary["health"]
        self._disk_full_eta_s = summary["disk_full_eta_s"]
        self._sites_health = summary["sites"]
        return {
            "available": available,
            "import_error": import_error,
            "streamonitor_path": _STREAMONITOR_PATH or "",
            "summary": summary,
            "models": models,
        }

    def _active_stats(self, active) -> float:
        """From the currently-recording files (real os.path.getsize -- the cached
        video_files_total_size can't show live speed) compute in ONE pass: live
        write speed (per-file growth), its rolling average, a ~16-min history for
        the sparkline, avg active fragment size (re-auth continuity signal), and
        the top recorders. `active` = list of (path, username, site)."""
        import os as _os, time as _t
        from collections import deque as _dq
        now = _t.monotonic()
        prev = getattr(self, "_speed_prev", None) or {}
        prev_t = getattr(self, "_speed_prev_time", None)
        cur: Dict[str, int] = {}
        grown = 0
        sized = []  # (size, username, site)
        for path, uname, site in active:
            try:
                sz = _os.path.getsize(path)
            except Exception:
                continue
            cur[path] = sz
            if path in prev and sz > prev[path]:
                grown += sz - prev[path]
            sized.append((sz, uname, site))
        self._speed_prev = cur
        self._speed_prev_time = now
        bps = (grown / (now - prev_t)) if (prev_t and now > prev_t) else 0.0
        self._download_bps = bps
        samples = getattr(self, "_speed_samples", None)
        if samples is None:
            samples = self._speed_samples = _dq(maxlen=90)
        if prev_t is not None:
            samples.append(bps)
        self._download_bps_avg = (sum(samples) / len(samples)) if samples else 0.0
        # sparkline history: ~16 min at a ~16s cadence (every 8th 2s build)
        hist = getattr(self, "_speed_hist", None)
        if hist is None:
            hist = self._speed_hist = _dq(maxlen=64)
        ctr = getattr(self, "_speed_hist_ctr", 0) + 1
        self._speed_hist_ctr = ctr
        if ctr % 8 == 0:
            hist.append(int(bps))
        szs = [s for s, _, _ in sized]
        self._avg_fragment_mb = (sum(szs) / len(szs) / 1e6) if szs else 0.0
        self._top_recorders = [
            {"username": u, "site": st, "mb": round(s / 1e6, 1)}
            for s, u, st in sorted(sized, reverse=True)[:6]
        ]
        return bps

    def _network_stats(self) -> float:
        """True internet DOWNLOAD rate: the busiest single interface's bytes_recv
        delta. NOT the sum across interfaces -- a VPN tunnel (WireGuard/Mullvad)
        carries the SAME payload as the physical NIC (decrypted on the TUN,
        encrypted on the wire), so summing double-counts it (~2x; verified live:
        Mullvad 11.9 + WiFi 12.2 MB/s for one ~12 MB/s stream). Max over per-NIC
        rates (loopback excluded) recovers the real rate. Distinct from the write
        speed (file growth), which lags ffmpeg's buffer. Time-gated ~1s so it's
        safe to call from BOTH get_snapshot and the cheap live_summary."""
        import time as _t
        from collections import deque as _dq
        now = _t.monotonic()
        prev_t = getattr(self, "_net_prev_time", None)
        if prev_t is not None and (now - prev_t) < 1.0:
            return getattr(self, "_network_bps", 0.0)  # too soon -> reuse last rate
        try:
            import psutil
            per = psutil.net_io_counters(pernic=True)
            cur = {nic: int(c.bytes_recv) for nic, c in per.items()
                   if "loopback" not in nic.lower() and nic.lower() != "lo"}
        except Exception:
            return getattr(self, "_network_bps", 0.0)
        prev = getattr(self, "_net_prev", None)  # dict {nic: bytes_recv}
        self._net_prev = cur
        self._net_prev_time = now
        if not isinstance(prev, dict) or not prev:  # first sample
            return getattr(self, "_network_bps", 0.0)
        dt = (now - prev_t) or 1.0
        # MAX single-interface rate, not the sum -- the VPN tunnel and the physical
        # NIC each carry the same payload, so summing double-counts (~2x).
        bps = 0.0
        for nic, v in cur.items():
            p = prev.get(nic)
            if p is not None and v >= p:
                r = (v - p) / dt
                if r > bps:
                    bps = r
        self._network_bps = bps
        samples = getattr(self, "_net_samples", None)
        if samples is None:
            samples = self._net_samples = _dq(maxlen=90)
        samples.append(bps)
        self._network_bps_avg = (sum(samples) / len(samples)) if samples else 0.0
        hist = getattr(self, "_net_hist", None)
        if hist is None:
            hist = self._net_hist = _dq(maxlen=64)
        ctr = getattr(self, "_net_hist_ctr", 0) + 1
        self._net_hist_ctr = ctr
        if ctr % 8 == 0:                 # ~ every 16s -> ~17 min of history at maxlen 64
            hist.append(int(bps))
        return bps

    def _site_health(self, sites: Dict[str, Dict[str, int]]) -> list:
        """Per-site mini RAG: red if online models exist but few record, amber on
        a notable ERROR share, else green. Sorted by recording desc."""
        out = []
        for site, d in sites.items():
            pub, rec, err = d["public"], d["recording"], d["error"]
            level = "green"
            if pub >= 4 and rec < pub * 0.5:
                level = "red"
            elif (pub >= 8 and rec < pub * 0.75) or (err >= 4 and err >= max(1, rec)):
                level = "amber"
            enabled = self.site_enabled(site)
            if not enabled:
                # An off site isn't unhealthy, it's parked — don't let it burn
                # red in the RAG strip and mask a site that really is broken.
                level = "off"
            out.append({"site": site, "recording": rec, "public": pub,
                        "error": err, "level": level, "enabled": enabled})
        out.sort(key=lambda x: -x["recording"])
        return out

    def _uptime(self) -> float:
        import time as _t
        bt = getattr(self, "_boot_time", None)
        if bt is None:
            bt = self._boot_time = _t.monotonic()
        return _t.monotonic() - bt

    def _disk_eta(self, free) -> Optional[float]:
        """Seconds until the recordings drive fills, from the NET fill rate
        (delta free bytes), which accounts for the user's merger deleting
        fragments -- not just the record write rate. None if not filling."""
        import time as _t
        if free is None:
            return None
        now = _t.monotonic()
        pf = getattr(self, "_disk_free_prev", None)
        pt = getattr(self, "_disk_free_prev_t", None)
        # Sample the net fill rate over a MEANINGFUL window (>=30s). Free-space
        # deltas across the ~2s snapshot cadence are far too noisy -- a single
        # bursty ffmpeg flush (or the merger's delete) gets extrapolated into a
        # wildly short ETA, which flapped the health pill to "degraded". Between
        # samples, reuse the last ETA.
        if pf is not None and pt is not None and (now - pt) < 30:
            return getattr(self, "_disk_full_eta_s", None)
        self._disk_free_prev = free
        self._disk_free_prev_t = now
        if pf is None or pt is None or now <= pt:
            return None
        fill = (pf - free) / (now - pt)   # +ve = filling up
        ema = getattr(self, "_disk_fill_ema", None)
        fill = (0.35 * fill + 0.65 * ema) if ema is not None else fill
        self._disk_fill_ema = fill
        return (free / fill) if fill > 1024 else None

    def _compute_health(self, s: Dict[str, Any]) -> Dict[str, Any]:
        """RAG health -- the worst of disk / data-flow / coverage / errors, with
        human reasons. Green = data flowing + disk OK; escalates on real trouble."""
        lvl = 0  # 0 green, 1 amber, 2 red
        reasons: List[str] = []
        used = s.get("disk_used_pct")
        if used is not None:
            if used >= 95:
                lvl = 2; reasons.append(f"disk {used:.0f}% full")
            elif used >= 85:
                lvl = max(lvl, 1); reasons.append(f"disk {used:.0f}% full")
        rec = s.get("recording", 0)
        avg = s.get("download_bps_avg") or 0
        if rec >= 3 and avg <= 0:
            lvl = 2; reasons.append("recordings not writing data")
        hist = s.get("status_hist", {}) or {}
        pub = hist.get("PUBLIC", 0); err = hist.get("ERROR", 0)
        if pub >= 12 and rec < pub * 0.55:
            lvl = max(lvl, 1); reasons.append(f"{pub - rec} online models not recording")
        online = pub + hist.get("PRIVATE", 0) + rec
        if err >= 10 and (online == 0 or err > online * 0.2):
            lvl = max(lvl, 1); reasons.append(f"{err} models in ERROR")
        eta = s.get("disk_full_eta_s")
        if eta is not None and eta < 3600:
            lvl = 2; reasons.append("disk fills in under 1h")
        elif eta is not None and eta < 6 * 3600:
            lvl = max(lvl, 1); reasons.append("disk fills in under 6h")
        return {"level": ["healthy", "degraded", "problem"][lvl], "reasons": reasons}

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
        # Carry the site with each bot so the per-site tally comes from THIS
        # pass rather than a cached one (see below).
        with self._lock:
            pairs = [(rm.site, rm.bot) for rm in self._models.values()]
        bots = [b for _, b in pairs]
        running = recording = 0
        total_bytes = 0
        status_hist: Dict[str, int] = {}
        sites_tally: Dict[str, Dict[str, int]] = {}
        for site, bot in pairs:
            if getattr(bot, "running", False):
                running += 1
            is_rec = bool(getattr(bot, "recording", False))
            if is_rec:
                recording += 1
            total_bytes += int(getattr(bot, "video_files_total_size", 0) or 0)
            name = getattr(getattr(bot, "sc", None), "name", "UNKNOWN")
            status_hist[name] = status_hist.get(name, 0) + 1
            _s = sites_tally.setdefault(
                site, {"total": 0, "recording": 0, "public": 0, "error": 0})
            _s["total"] += 1
            if name == "PUBLIC":
                _s["public"] += 1
            elif name == "ERROR":
                _s["error"] += 1
            if is_rec:
                _s["recording"] += 1
        # Per-site health from the tally we JUST computed.
        #
        # This used to read the cached `self._sites_health`, which only
        # get_snapshot() writes — and get_snapshot only runs while the heavy
        # /api/live/status endpoint is being polled. So the payload mixed a
        # live `recording` total with a per-site breakdown that could be
        # minutes stale: observed 63 recording against a site tally summing to
        # 60, with Chaturbate frozen at an early-ramp-up "1/26 red" while it
        # was actually running 21 captures. A site strip that reports a healthy
        # site as red is worse than no strip at all.
        sites_health = self._site_health(sites_tally)
        out: Dict[str, Any] = {
            "total": len(bots), "running": running, "recording": recording,
            "total_bytes": total_bytes, "status_hist": status_hist,
            "download_bps": getattr(self, "_download_bps", 0.0),  # live write speed (get_snapshot computes it)
            "download_bps_avg": getattr(self, "_download_bps_avg", 0.0),
            "network_bps": self._network_stats(),                 # true off-the-wire rate (sampled here too)
            "network_bps_avg": getattr(self, "_network_bps_avg", 0.0),
            "network_hist": list(getattr(self, "_net_hist", [])),
            "avg_fragment_mb": getattr(self, "_avg_fragment_mb", 0.0),
            "top_recorders": getattr(self, "_top_recorders", []),
            "speed_hist": list(getattr(self, "_speed_hist", [])),
            "sites": sites_health,
            "alerts": recent_alerts(),
            "uptime_s": self._uptime(),
            "disk_full_eta_s": getattr(self, "_disk_full_eta_s", None),
            "health": getattr(self, "_health", {"level": "healthy", "reasons": []}),
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
