# streamonitor/bot.py
# Fully fixed bot implementation with thread safety and memory leak prevention

from __future__ import unicode_literals
import os
import traceback
from urllib.parse import urljoin
import m3u8
import warnings
import filelock
import logging
from time import sleep, monotonic, time as _wall_clock
from datetime import datetime
from threading import Thread, Event, Lock, RLock, BoundedSemaphore
from typing import Optional, List, Dict, Any, Set, Union, Callable, Type

from streamonitor.enums import Status, Gender, GENDER_DATA, COUNTRIES
import streamonitor.log as log
import parameters
from parameters import (
    DOWNLOADS_DIR, WANTED_RESOLUTION, WANTED_RESOLUTION_PREFERENCE, 
    CONTAINER, HTTP_USER_AGENT, VERIFY_SSL
)
from streamonitor.downloaders.ffmpeg import getVideoFfmpeg
from streamonitor.models import VideoData
from streamonitor.utils.cf_session import CFSessionManager
from streamonitor.utils import proxy_pool
from urllib.parse import urljoin
import urllib3

# Import termcolor for colored status messages
try:
    from termcolor import colored
    TERMCOLOR_AVAILABLE = True
except ImportError:
    TERMCOLOR_AVAILABLE = False
    def colored(text: str, color: Optional[str] = None, attrs: Optional[List[str]] = None) -> str:
        return text

# ── Recordings base dir availability ─────────────────────────────────────────
#
# DOWNLOADS_DIR can point at a removable/secondary drive (e.g. E:\F\Recordings).
# If that drive is not mounted, os.makedirs() in genOutFilename() raises
# FileNotFoundError [WinError 3] for EVERY bot on EVERY status change -- the run
# loop catches it, sleeps, and retries forever, so nothing records and the log
# fills with identical tracebacks (753 of them in one observed session).
#
# Recordings must stay on the configured drive -- we deliberately do NOT spill
# onto the system disk, because a half-here/half-there library is worse than a
# paused one, and C: filling up takes the whole machine down with it. So when
# the drive is missing we HOLD: refuse to start new captures, say so once, and
# keep re-checking so recording resumes by itself the moment it is back.
_base_lock = Lock()
_base_state: Dict[str, Any] = {"ok": None, "checked_at": 0.0, "reason": ""}
_BASE_RECHECK_S = 30.0
# Module-level logger: propagates to the root handler Harvestr installs, so
# these land in logs/live-errors.log alongside the per-bot messages.
_dirlog = logging.getLogger("streamonitor.recordings_dir")


class ModelFolderUnavailable(OSError):
    """One model's output folder is unusable, but the drive itself is fine.

    Distinct from RecordingsDirUnavailable (whole drive missing): this is a
    per-model condition, so the rest of the fleet keeps recording normally.
    """


def _folder_is_corrupt(folder: str) -> bool:
    """True if `folder` exists but rejects a trivial file create.

    Deliberately probes rather than trusting the original error: a create can
    also fail for transient reasons (AV scanner, momentary sharing violation),
    and permanently parking a model over a blip would be worse than the
    retry-loop this replaces.
    """
    probe = os.path.join(folder, ".write-probe.tmp")
    try:
        with open(probe, "w") as f:
            f.write("")
        os.remove(probe)
        return False
    except OSError:
        return True
    except Exception:
        return False


class RecordingsDirUnavailable(OSError):
    """The configured recordings drive is not currently reachable.

    Distinct type so the bot run loop can hold quietly instead of treating it
    as a per-model download error worth a traceback and an error-count bump.
    """


def _recordings_base_ok(force: bool = False) -> bool:
    """Is DOWNLOADS_DIR reachable/creatable right now?

    Cached for _BASE_RECHECK_S so hundreds of bots asking at once cost one
    stat, while a remount is still picked up on its own within ~30s.
    """
    now = monotonic()
    with _base_lock:
        if (not force and _base_state["ok"] is not None
                and now - _base_state["checked_at"] < _BASE_RECHECK_S):
            return _base_state["ok"]

        try:
            os.makedirs(DOWNLOADS_DIR, exist_ok=True)
            ok, reason = True, ""
        except OSError as e:
            ok, reason = False, str(e)

        # Log only on transition, so a long outage costs one line, not one per
        # bot per retry.
        if ok is not _base_state["ok"]:
            if ok:
                if _base_state["ok"] is not None:
                    _dirlog.info(
                        f"Recordings drive {DOWNLOADS_DIR!r} is back — resuming")
            else:
                _dirlog.warning(
                    f"Recordings drive {DOWNLOADS_DIR!r} is unreachable ({reason}). "
                    f"Holding all recording until it returns — nothing will be "
                    f"written to the system disk.")
        _base_state.update(ok=ok, checked_at=now, reason=reason)
        return ok


def _recordings_base() -> str:
    """The recordings root, or raise if its drive is currently missing."""
    if not _recordings_base_ok():
        raise RecordingsDirUnavailable(
            f"recordings drive unavailable: {DOWNLOADS_DIR} "
            f"({_base_state['reason']})")
    return DOWNLOADS_DIR


def recordings_dir_status() -> Dict[str, Any]:
    """Availability of the recordings drive, for the UI/health panel."""
    # Resolve first: before any recording has been attempted the cached state
    # is still None, and reporting that as "unavailable" would show a healthy
    # drive as missing.
    ok = _recordings_base_ok()
    with _base_lock:
        return {"path": DOWNLOADS_DIR,
                "available": ok,
                "reason": _base_state["reason"]}


# Disable SSL warnings if SSL verification is disabled
if not VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    warnings.warn(
        "SSL verification is disabled. This is insecure and should only be used for testing.",
        UserWarning, stacklevel=2
    )

# +-- Deletion kill-switch ---------------------------------------------------
# DEFAULT: the recorder deletes NOTHING inside the recordings tree.
#
# An automatic retention pass once removed 6,743 files / 453 GB in 25 hours --
# more than it left behind. Every unlink under a model's output folder now goes
# through this switch, INCLUDING the "it is only a zero-byte file" cleanups: a
# 0-byte file costs nothing to keep, and "safe to delete" is precisely the
# judgement that went wrong before.
#
# Set HARVESTR_ALLOW_DELETES=1 to restore the old cleanup behaviour. Scratch
# outside the recordings tree (M3U8_TMP, rolling playlists, browser profiles)
# is unaffected -- none of it is a capture.
ALLOW_RECORDING_DELETES = (
    os.environ.get("HARVESTR_ALLOW_DELETES", "").strip() == "1")


def _may_delete(path, logger=None, why=""):
    """True only if deleting inside the recordings tree is explicitly enabled."""
    if ALLOW_RECORDING_DELETES:
        return True
    if logger is not None:
        try:
            logger.debug(f"delete suppressed ({why or 'no-delete policy'}): {path}")
        except Exception:
            pass
    return False


# Global locks for thread-safe operations
_print_lock = Lock()
_filename_lock = RLock()  # Reentrant lock for nested filename operations


# Concurrent-recording cap. DEFAULT IS UNLIMITED (0): the per-recording ffmpeg
# watchdog (hls.py) now kills dead/ghost streams within ~30s, so the real
# ceiling is just "how many models are actually live" rather than an unbounded
# pile of hung ffmpeg. Set HARVESTR_MAX_RECORDINGS=N to re-impose a hard cap of
# N simultaneous recordings (e.g. on a very RAM-constrained box).
def _max_recordings_default() -> int:
    try:
        return int((os.environ.get("HARVESTR_MAX_RECORDINGS") or "0").strip())
    except Exception:
        return 0


MAX_CONCURRENT_RECORDINGS = _max_recordings_default()
# None => unlimited (no semaphore); a positive value caps concurrent recordings.
_recording_sem = BoundedSemaphore(MAX_CONCURRENT_RECORDINGS) if MAX_CONCURRENT_RECORDINGS > 0 else None


def _record_under_cap(bot: 'Bot', video_url: str, file: str) -> bool:
    """Run bot.getVideo, optionally under the global concurrent-recording cap.

    Unlimited by default (MAX_CONCURRENT_RECORDINGS<=0): just records — the
    ffmpeg watchdog bounds dead streams. With a positive cap it waits
    (responsively, so stop()/quit stays honored) for a free slot, holds it for
    the whole recording, and always releases it; returns False if the bot was
    stopped/quit before a slot freed up."""
    if _recording_sem is None:
        return bool(bot.getVideo(bot, video_url, file))
    acquired = False
    while bot.running and not bot.quitting:
        acquired = _recording_sem.acquire(timeout=2)
        if acquired:
            break
    if not acquired:
        return False
    try:
        return bool(bot.getVideo(bot, video_url, file))
    finally:
        _recording_sem.release()


# Global set of loaded site classes for upstream compatibility (used by BulkStatusManager)
LOADED_SITES: Set[Type['Bot']] = set()


class Bot(Thread):
    loaded_sites: Set[Type['Bot']] = set()
    username: Optional[str] = None
    site: Optional[str] = None
    siteslug: Optional[str] = None
    aliases: List[str] = []
    # When True (set by LiveManager ONLY during its startup restore of 1000+
    # saved models), Bot.__init__ SKIPS the synchronous folder scan
    # (cache_file_list) so boot isn't blocked by per-model disk I/O; a
    # background sweeper runs the scans just after boot. Default False keeps the
    # native CLI path (and UI-created bots) scanning synchronously as before.
    defer_init_scan: bool = False
    # When True (set by LiveManager ONLY during its startup restore), bulk-update
    # bots do NOT self-poll getStatus at NOTRUNNING -- their startup status comes
    # from the bulk poller, avoiding a 1000+ getStatus burst that stalled boot
    # (port unreachable for minutes). Reset to False after restore so UI-ADDED
    # bulk bots self-poll once to get their initial status promptly; the bulk
    # poller can lag a freshly-added model, which left newly added CB/SC/CS
    # models stuck NOTRUNNING (online but never recording).
    suppress_boot_poll: bool = False
    ratelimit: bool = False
    bulk_update: bool = False  # Override True in sites that support bulk status updates
    # When True, this site's stream tokens are bound to the exit IP (e.g.
    # StripChat/doppiocdn), so a VPN rotation invalidates the in-flight token and
    # the capture CANNOT ride through -- it must restart to fetch a fresh token on
    # the new IP. The LiveManager restarts these recordings the instant a rotation
    # completes (fast recovery) instead of waiting ~60s for the stall watchdog.
    # Sites whose segments are NOT IP-bound (e.g. Chaturbate) leave this False and
    # ride through the rotation gap on the same file.
    tokens_ip_bound: bool = False
    url: str = "javascript:void(0)"
    recording: bool = False
    sleep_on_private: int = 5
    sleep_on_offline: int = 8
    sleep_on_long_offline: int = 60   # Was 15s — at 200+ LONG_OFFLINE bots
                                       # × 1/15s, that's 13 req/sec just for
                                       # offline polling, which contributed
                                       # to WinError 1450 socket-exhaustion.
                                       # 60s = 3 req/sec instead.
    sleep_on_error: int = 60          # Was 20s — back off harder so 500
                                       # erroring bots aren't pinging the
                                       # net every 20s.
    sleep_on_ratelimit: int = 180
    long_offline_timeout: int = 300   # Back to 5 min so bots take longer
                                       # to enter long-offline mode (gives
                                       # them more chances to recover)
    previous_status: Optional[Status] = None
    _GENDER_MAP: Dict[str, Gender] = {}  # Override in site subclasses to map API gender strings to Gender enum

    def __init_subclass__(cls, **kwargs):
        """Auto-register site subclasses when they are defined."""
        super().__init_subclass__(**kwargs)
        if cls.site is not None:
            Bot.loaded_sites.add(cls)
            LOADED_SITES.add(cls)

    # Manager registry for auto-removal of deleted models
    _manager_instance = None

    headers: Dict[str, str] = {
        "User-Agent": HTTP_USER_AGENT
    }

    # Browser-ish navigation headers (upstream a63e161 "Fix SC status").
    # StripChat's status endpoint started returning junk to requests that look
    # like a bare API client; sending the same Sec-Fetch/Accept set a real
    # browser tab sends makes it answer normally again. Merge into `headers`
    # per-request rather than globally — other sites are happy without it.
    html_headers: Dict[str, str] = {
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en,en-US;q=0.9,en-US;q=0.8,en;q=0.7',
        'Pragma': 'no-cache',
        'Priority': 'u=4',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'cross-site',
        'Sec-Fetch-User': '?1',
        'Upgrade-Insecure-Requests': '1',
    }

    status_messages: Dict[Status, str] = {
        Status.UNKNOWN: colored("Unknown error", "red"),
        Status.PUBLIC: colored("Channel online", "green", attrs=["bold"]),
        Status.ONLINE: colored("Connected, waiting for stream", "cyan", attrs=["bold"]),
        Status.OFFLINE: colored("No stream", "yellow"),
        Status.PRIVATE: colored("Private show", "magenta"),
        Status.DELETED: colored("Model account deleted", "red", attrs=["bold"]),
        Status.RATELIMIT: colored("Rate limited", "red", attrs=["bold"]),
        Status.NOTEXIST: colored("Nonexistent user", "red"),
        Status.LONG_OFFLINE: colored("Long offline", "yellow", attrs=["dark"]),
        Status.NOTRUNNING: colored("Not running", "white"),
        Status.ERROR: colored("Error on downloading", "red", attrs=["bold"]),
        Status.RESTRICTED: colored("Model is restricted, maybe geo-block", "red"),
        Status.CLOUDFLARE: colored("Cloudflare", "blue"),
    }

    # Class-level logger cache to prevent memory leaks
    _logger_cache: Dict[str, log.Logger] = {}
    _logger_cache_lock: Lock = Lock()

    def __init__(self, username: str) -> None:
        super().__init__()
        self.daemon = True
        self.username = username
        self.gender: Gender = Gender.UNKNOWN
        self.country: Optional[str] = None
        
        # Use cached logger to prevent memory leaks
        self.logger = self._get_or_create_logger()

        self.cookies: Optional[Any] = None
        self.impersonate: Optional[str] = None
        self.cookieUpdater: Optional[Callable[[], bool]] = None
        self.cookie_update_interval: int = 0
        # Sticky proxy from the optional rotating pool. None when no pool is
        # configured -> direct connection (unchanged behaviour).
        self.proxy: Optional[str] = proxy_pool.get_proxy(
            f"[{self.siteslug}] {self.username}")
        self.session: CFSessionManager = CFSessionManager(
            logger=self.logger,
            bot_id=f"[{self.siteslug}] {self.username}",
            verify=VERIFY_SSL,
            proxy=self.proxy,
        )

        self._cookie_thread: Optional[Thread] = None
        self._cookie_thread_stop: Event = Event()
        self._state_lock: Lock = Lock()

        self.lastInfo: Dict[str, Any] = {}
        self.running: bool = False
        self.quitting: bool = False
        self.sc: Status = Status.NOTRUNNING
        self.getVideo: Callable = getVideoFfmpeg
        self.stopDownload: Optional[Callable[[], None]] = None
        self.recording: bool = False
        # Consecutive failed record cycles (no stream URL, or a recording that
        # produced no data). Most single failures are the model leaving public
        # between the status poll and the fetch (offline/private) or a transient
        # CDN/DNS blip -- those self-heal on the next poll, so we DON'T flap the
        # card to ERROR or log at ERROR for them; we only escalate after several
        # in a row (a genuinely stuck-online model). Reset on any successful record.
        self._consec_dl_fail: int = 0
        # Path of the file the active recording is currently writing to (set by the
        # downloader). Lets the LiveManager measure real-time write speed by
        # os.path.getsize on it, since video_files_total_size is only cache-updated.
        self._current_output: Optional[str] = None
        self.video_files: List[VideoData] = []
        self.video_files_total_size: int = 0
        # Guards the (list, size) rebind in cache_file_list so concurrent
        # callers (bot thread post-recording, HTTP thread, the startup sweeper)
        # can't leave video_files and video_files_total_size mutually
        # inconsistent. _video_files_scanned lets the startup sweeper skip a bot
        # already scanned by its own post-recording call (no double scan).
        self._video_lock: Lock = Lock()
        self._video_files_scanned: bool = False
        # Whether this bot was created during LiveManager's bulk startup restore
        # (suppress_boot_poll is True only then). Boot bots stagger their first
        # poll to avoid a thundering herd; a bot ADDED later via the UI is a
        # single bot with no herd, so it skips the stagger and records promptly.
        self._is_boot_bot: bool = bool(type(self).suppress_boot_poll)
        self.isRetryingDownload: bool = False
        
        self.verify_with_ffprobe: bool = True
        self.clean_failed_temp: bool = True
        
        self._consecutive_errors: int = 0
        self._max_consecutive_errors: int = 20

        # 2026-05-09: wake-from-sleep + offline-timer reset support.
        # `_wake_event` lets `restart()` interrupt a sleep that would
        # otherwise tie up the bot for `sleep_on_long_offline` (10+ min)
        # or `sleep_on_ratelimit` (exponential). `_offline_time` is the
        # accumulator that decides when a bot transitions OFFLINE →
        # LONG_OFFLINE; it was a local in run() so restart() couldn't
        # reset it, meaning long-offline bots stayed in long-sleep
        # cadence forever after the user clicked "Start all live".
        self._wake_event = Event()
        self._offline_time: float = 0

        # Skipped during LiveManager's bulk startup restore (defer_init_scan) so
        # 1000+ disk scans don't block boot; the background sweeper fills these
        # in just after. Always runs on the native CLI path and for UI-created
        # bots (a single cheap scan).
        if not type(self).defer_init_scan:
            try:
                self.cache_file_list()
            except Exception as e:
                self.logger.warning(f"Failed to cache file list during init: {e}")

    def _get_or_create_logger(self) -> log.Logger:
        """
        Get or create logger from cache to prevent memory leaks.
        Multiple bot instances for same user/site share one logger.
        """
        logger_key = f"[{self.siteslug}] {self.username}"
        
        with Bot._logger_cache_lock:
            if logger_key not in Bot._logger_cache:
                Bot._logger_cache[logger_key] = log.Logger(logger_key, self)
            return Bot._logger_cache[logger_key]

    @classmethod
    def cleanup_logger_cache(cls, logger_key: Optional[str] = None) -> None:
        """
        Clean up logger cache. Call when removing a bot.
        
        Args:
            logger_key: Specific logger to remove, or None to clear all
        """
        with cls._logger_cache_lock:
            if logger_key:
                if logger_key in cls._logger_cache:
                    logger = cls._logger_cache.pop(logger_key)
                    if hasattr(logger, 'handlers'):
                        logger.handlers.clear()
            else:
                # Clear all loggers
                for logger in cls._logger_cache.values():
                    if hasattr(logger, 'handlers'):
                        logger.handlers.clear()
                cls._logger_cache.clear()

    def getLogger(self) -> log.Logger:
        """Legacy method for compatibility."""
        return self.logger

    @property
    def country_data(self) -> Optional[Dict[str, str]]:
        """Get country data (name, flag) for this model's country code."""
        if self.country:
            return COUNTRIES.get(self.country.upper())
        return None

    @property
    def gender_data(self) -> Optional[Dict[str, Any]]:
        """Get gender display data (name, icon, color) for this model's gender."""
        return GENDER_DATA.get(self.gender)

    def setStatus(self, status: Status, gender: Optional[Gender] = None, country: Optional[str] = None) -> None:
        """Set status from bulk status update (used by BulkStatusManager).
        Also updates gender/country if provided.
        """
        if gender is not None:
            self.gender = gender
        if country is not None:
            self.country = country
        self.sc = status
        if self.sc != self.previous_status:
            self.log(self.status())
            self.previous_status = self.sc

    def setUsername(self, username: str, move_folder: bool = False) -> None:
        """Update username (e.g. resolved from room_id, or the model renamed).

        `move_folder` carries the model's existing recordings over to the new
        name (upstream 68a9c8b). Off by default because the common caller is
        room-id resolution, where the folder was only just derived and there is
        nothing to move.
        """
        if not username or username == self.username:
            return
        old = self.username
        old_folder = self.outputFolder if move_folder else None
        self.username = username
        # Re-derive the identity-dependent bits, or the bot keeps logging under
        # the old name and linking to a dead profile URL.
        try:
            self.logger = self._get_or_create_logger()
        except Exception:
            pass
        try:
            self.url = self.getWebsiteURL()
        except Exception:
            pass

        if old_folder:
            new_folder = self.outputFolder
            try:
                if (os.path.isdir(old_folder) and not os.path.exists(new_folder)
                        and os.path.abspath(old_folder) != os.path.abspath(new_folder)):
                    os.rename(old_folder, new_folder)
                    self.logger.info(f"Moved recordings {old_folder} -> {new_folder}")
            except OSError as e:
                # Non-fatal: keep recording under the new name, old files stay
                # where they are rather than risking a half-moved folder.
                self.logger.warning(f"Could not move recordings folder: {e}")
        try:
            self.cache_file_list()
        except Exception:
            pass
        self.logger.info(f"Username updated: {old} -> {username}")
    
    def get_site_color(self) -> tuple[str, list[str]]:
        """Default color scheme for sites that don't override this method."""
        return ("white", [])
    
    def log(self, message: str) -> None:
        """Thread-safe logging with print lock."""
        with _print_lock:
            self.logger.info(message)

    def restart(self) -> None:
        with self._state_lock:
            if not self.running:
                self.logger.verbose("Starting bot...")

            # 2026-05-09: clear ALL stale-state values that hold the bot
            # in extended backoff. Previously this only reset OFFLINE →
            # UNKNOWN, leaving LONG_OFFLINE (sleep_on_long_offline =
            # 10-20 min), RATELIMIT (exponential), and ERROR (up to 300s
            # extra) bots stuck in their long-sleep loops even after the
            # user clicked "Start all live". The result: clicking Start
            # was effectively a no-op for any bot that had already
            # backed off, so most of the fleet stayed silent.
            #
            # Now any non-PUBLIC/non-PRIVATE state gets cleared to
            # UNKNOWN so the next iteration of the main loop does a
            # fresh getStatus() call rather than continuing a backoff
            # that the user is explicitly trying to abort.
            stale_states = (Status.OFFLINE, Status.LONG_OFFLINE,
                            Status.RATELIMIT, Status.ERROR,
                            Status.UNKNOWN, Status.NOTRUNNING)
            if self.sc in stale_states:
                prev = self.sc
                # 2026-05-09 critical: reset target is NOTRUNNING, not
                # UNKNOWN. The main loop only calls getStatus() if
                # `not self.bulk_update or self.sc == Status.NOTRUNNING`.
                # Setting sc=UNKNOWN on a bulk-update bot (Chaturbate,
                # StripChat-bulk, CamSoda) skips the getStatus() call —
                # the bot expects BulkStatusManager to update it via
                # setStatus(), but LiveManager doesn't run that manager.
                # NOTRUNNING is the correct "I just started, please poll
                # me" sentinel. The first main-loop iteration will call
                # getStatus() and set a real status; from there the bot
                # proceeds normally.
                self.sc = Status.NOTRUNNING
                self._consecutive_errors = 0
                # Reset the offline-time accumulator so a bot that's
                # been LONG_OFFLINE for hours doesn't immediately
                # transition back to LONG_OFFLINE on the very next
                # iteration. Pair with the wake event below.
                self._offline_time = 0
                if prev != Status.NOTRUNNING:
                    self.logger.verbose(
                        f"Resetting stale state {prev.name} → NOTRUNNING "
                        "(forcing fresh status check)"
                    )

            self.running = True

            # Reset previous_status to ensure fresh status logging after restart
            self.previous_status = None

            # Reset offline timing on restart
            self._last_restart_time = datetime.now().timestamp()

            # Fire the wake signal so any sleep currently in flight
            # (sleep_on_long_offline = 10+ min, sleep_on_ratelimit
            # exponential, etc.) breaks out immediately and the main
            # loop runs a fresh getStatus() right away.
            self._wake_event.set()
            
            # Ensure the thread is actually alive
            if not self.is_alive():
                # A never-started thread (ident is None) reaching the restart
                # path is the NORMAL first start of a freshly-added/loaded bot,
                # not a failure. Only a thread that actually ran and died is a
                # real problem. Previously both logged the same WARNING, which
                # produced 200+ false "Thread was dead" alarms when models were
                # added/started en masse.
                never_started = self.ident is None
                try:
                    if never_started:
                        self.logger.verbose("Starting bot thread")
                    else:
                        self.logger.warning("Thread was dead during restart, starting new thread")
                    # Reset thread state
                    self.quitting = False
                    # Start the thread if it's not alive
                    self.start()
                except RuntimeError as e:
                    if "threads can only be started once" in str(e):
                        self.logger.error("Cannot restart dead thread - this bot needs to be recreated")
                    else:
                        self.logger.error(f"Error starting thread: {e}")
                except Exception as e:
                    self.logger.error(f"Unexpected error starting thread: {e}")

    def stop(self, a: Any = None, b: Any = None, thread_too: bool = False) -> None:
        with self._state_lock:
            if self.running:
                self.log(colored("Stopping...", "red", attrs=["bold"]))
                if self.stopDownload:
                    try:
                        self.stopDownload()
                    except Exception as e:
                        self.logger.warning(f"Error calling stopDownload: {e}")
                self.running = False
            if thread_too:
                self.quitting = True

    def getStatus(self) -> Status:
        return Status.UNKNOWN

    def debug(self, message: str, filename: Optional[str] = None) -> None:
        if parameters.DEBUG:
            self.logger.debug(message)
            if not filename:
                filename = os.path.join(self.outputFolder, 'debug.log')
            try:
                os.makedirs(os.path.dirname(filename), exist_ok=True)
                with open(filename, 'a+', encoding='utf-8') as debugfile:
                    debugfile.write(message + '\n')
            except Exception as e:
                self.logger.debug(f"Failed to write debug log: {e}")

    def status(self) -> str:
        base_message = self.status_messages.get(self.sc) or self.status_messages.get(Status.UNKNOWN)
        
        if "VR" in self.siteslug:
            if self.sc == Status.PUBLIC:
                message = colored("LIVE VR STREAM!", "red", attrs=["bold"])
            elif self.sc == Status.PRIVATE:
                message = colored("VR Private Show", "magenta", attrs=["bold"])
            elif self.sc == Status.OFFLINE:
                message = colored("No stream", "yellow")
            elif self.sc == Status.ERROR:
                message = colored("VR Error", "red", attrs=["bold"])
            else:
                message = base_message
        else:
            if self.sc == Status.PUBLIC:
                message = colored("Channel online", "green", attrs=["bold"])
            elif self.sc == Status.PRIVATE:
                message = colored("Private show", "magenta")
            elif self.sc == Status.OFFLINE:
                message = base_message
            elif self.sc == Status.ERROR:
                message = colored("Error on downloading", "red", attrs=["bold"])
            elif self.sc == Status.RATELIMIT:
                message = colored("Rate limited", "red", attrs=["bold"])
            elif self.sc == Status.NOTEXIST:
                message = colored("Nonexistent user", "red")
            elif self.sc == Status.CLOUDFLARE:
                message = colored("Cloudflare", "blue")
            else:
                message = base_message
        
        if self.sc == Status.NOTEXIST:
            with self._state_lock:
                self.running = False
        return message

    def getWebsiteURL(self) -> str:
        return "javascript:void(0)"

    def cache_file_list(self) -> None:
        videos_folder = self.outputFolder
        _videos = []
        _total_size = 0
        if os.path.isdir(videos_folder):
            try:
                for file in os.scandir(videos_folder):
                    if file.is_dir():
                        continue
                    ext = os.path.splitext(file.name)[1][1:]
                    if ext not in ['mp4', 'mkv', 'webm', 'mov', 'avi', 'wmv', 'ts']:
                        continue
                    try:
                        video = VideoData(file, self.username)
                        _total_size += video.filesize
                        _videos.append(video)
                    except Exception as e:
                        self.logger.debug(f"Error processing video file {file.name}: {e}")
            except Exception as e:
                self.logger.warning(f"Error scanning video folder: {e}")
        # The scan above ran WITHOUT the lock (it only touches locals); take it
        # just for the tiny rebind so readers never see list/size out of sync,
        # and mark the bot scanned so the startup sweeper skips it.
        with self._video_lock:
            self.video_files = _videos
            self.video_files_total_size = _total_size
            self._video_files_scanned = True

    def _sleep(self, time: Union[int, float]) -> None:
        """Interruptible sleep that checks for quit/stop/wake signals.

        2026-05-09: also breaks out when self._wake_event is set, which
        restart() fires to force a sleeping bot (LONG_OFFLINE or
        RATELIMIT backoff) to immediately re-check status. Without this,
        clicking "Start all live" had no effect on bots already in
        long-sleep loops — they finished their multi-minute sleep
        before noticing the restart."""
        end_time = datetime.now().timestamp() + time
        while datetime.now().timestamp() < end_time:
            if self.quitting or not self.running:
                return
            if self._wake_event.is_set():
                self._wake_event.clear()
                return
            remaining = end_time - datetime.now().timestamp()
            sleep(min(1, max(0, remaining)))

    def _start_cookie_updater(self) -> None:
        if self.cookie_update_interval <= 0 or self.cookieUpdater is None:
            return
            
        if self._cookie_thread is not None and self._cookie_thread.is_alive():
            return
            
        self._cookie_thread_stop.clear()
        
        def update_cookie():
            self.logger.debug("Cookie updater thread started")
            while not self._cookie_thread_stop.is_set():
                try:
                    self._sleep(self.cookie_update_interval)
                    if self._cookie_thread_stop.is_set():
                        break
                    
                    if not self.recording or not self.running:
                        break
                        
                    ret = self.cookieUpdater()
                    if ret:
                        self.debug('Updated cookies')
                    else:
                        self.logger.warning('Failed to update cookies')
                except Exception as e:
                    self.logger.exception(f"Cookie updater error: {e}")
                    break
            self.logger.debug("Cookie updater thread stopped")
        
        self._cookie_thread = Thread(target=update_cookie, daemon=True)
        self._cookie_thread.start()

    def _stop_cookie_updater(self) -> None:
        if self._cookie_thread is not None:
            self._cookie_thread_stop.set()
            self._cookie_thread = None

    def _is_zero_or_missing(self, path: str) -> bool:
        try:
            return (not os.path.exists(path)) or os.path.getsize(path) == 0
        except Exception:
            return True

    def _guess_temp_candidates(self, final_path: str) -> List[str]:
        stem, _ = os.path.splitext(final_path)
        folder = os.path.dirname(final_path)
        return [
            f"{stem}.tmp.ts",
            f"{stem}.ts.tmp",
            f"{stem}.segment.tmp",
            f"{stem}.part",
            f"{stem}.tmp",
            os.path.join(folder, "ffmpeg2pass-0.log"),
            os.path.join(folder, "ffmpeg2pass-0.log.mbtree"),
        ]

    def _post_download_cleanup(self, final_path: str, ok: bool) -> bool:
        """Verify final file and clean up temporary files on failure."""
        try:
            # For HLS downloads, check the actual .tmp.ts file instead of .mkv
            actual_file = final_path
            stem, ext = os.path.splitext(final_path)
            tmp_ts_file = stem + '.tmp.ts'
            
            # If .tmp.ts file exists, that's the actual output file for HLS
            if os.path.exists(tmp_ts_file) and not os.path.exists(final_path):
                actual_file = tmp_ts_file
            
            if ok and self._is_zero_or_missing(actual_file):
                self.logger.error(f"Output file is 0 KB or missing: {actual_file}")
                if os.path.exists(actual_file):
                    try:
                        if _may_delete(actual_file, self.logger, "output file"):
                            os.remove(actual_file)
                        self.logger.info("Removed zero-byte output file")
                    except Exception as e:
                        self.logger.warning(f"Failed to remove zero-byte file: {e}")
                ok = False
                self.isRetryingDownload = True

            if not ok and self.clean_failed_temp:
                # Clean up the actual file that was created
                if os.path.exists(actual_file) and self._is_zero_or_missing(actual_file):
                    try:
                        if _may_delete(actual_file, self.logger, "output file"):
                            os.remove(actual_file)
                        self.logger.info("Removed failed output file")
                    except Exception as e:
                        self.logger.warning(f"Failed to remove failed output: {e}")

                for tmp in self._guess_temp_candidates(final_path):
                    if not os.path.exists(tmp):
                        continue
                    
                    try:
                        should_delete = False
                        try:
                            with open(tmp, "rb") as f:
                                sniff = f.read(512)
                            if len(sniff) == 0:
                                should_delete = True
                                self.logger.debug(f"Temp file is empty: {os.path.basename(tmp)}")
                            elif b"<html" in sniff.lower() or b"<!doctype" in sniff.lower():
                                should_delete = True
                                self.logger.warning(f"Temp file contains HTML: {os.path.basename(tmp)}")
                        except Exception:
                            should_delete = True
                        
                        if should_delete:
                            if _may_delete(tmp, self.logger, "junk temp"):
                                os.remove(tmp)
                            self.logger.debug(f"Cleaned up temp file: {os.path.basename(tmp)}")
                    except Exception as e:
                        self.logger.debug(f"Failed to clean temp file {tmp}: {e}")
        
        except Exception as e:
            self.logger.warning(f"Error in post-download cleanup: {e}")
        
        return ok

    def _download_once(self) -> bool:
        try:
            video_url = self.getVideoUrl()
            if video_url is None:
                self.logger.error("Failed to get video URL")
                return False
            
            self.log(colored('Started downloading show', "green", attrs=["bold"]))
            self.recording = True
            try:
                file = self.genOutFilename()
            except ModelFolderUnavailable as e:
                # Per-model folder corrupted on disk (see genOutFilename).
                self.recording = False
                if not getattr(self, "_folder_corrupt_logged", False):
                    self._folder_corrupt_logged = True
                    self.logger.error(f"{e} — run chkdsk on the recordings drive")
                return False
            except RecordingsDirUnavailable:
                # Drive detached -- hold rather than spill to the system disk.
                # Already logged once globally by _recordings_base_ok().
                self.recording = False
                return False
            ok = False
            
            try:
                ok = _record_under_cap(self, video_url, file)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                self.logger.error(f"Download error: {e}")
                ok = False
            finally:
                self.recording = False
                self.stopDownload = None

                try:
                    ok = self._post_download_cleanup(file, ok)
                except Exception as e:
                    self.logger.warning(f"Cleanup error: {e}")

                try:
                    self.cache_file_list()
                except Exception as e:
                    self.logger.warning(f"Failed to update file cache: {e}")
            
            if ok:
                self.log(colored('Recording ended successfully', "green", attrs=["bold"]))
                self._consecutive_errors = 0
            else:
                self.log(colored('Recording failed', "red", attrs=["bold"]))
                self._consecutive_errors += 1
            
            return ok
            
        except Exception as e:
            self.logger.exception(f"Unexpected error in _download_once: {e}")
            self._consecutive_errors += 1
            return False

    def run(self) -> None:
        self.logger.verbose("Bot thread started, waiting for start signal...")
        
        try:
            while not self.quitting:
                if self.running:
                    break
                sleep(1)
            
            if self.quitting:
                self.logger.verbose("Bot quit before starting")
                return

            # Startup jitter: with hundreds of bots restarting at once
            # (webui boot), all firing their first status check
            # simultaneously exhausts the Windows socket pool
            # (WinError 1450 "insufficient system resources" was the
            # actual error that was silent because str(WindowsError)=='').
            # Spread first-polls across 5 minutes so the load comes in at
            # ~2-3/sec instead of a thunderstorm. Bots that were already
            # PUBLIC last save get a SHORT stagger (so live recordings
            # resume quickly); bots that were ERROR/OFFLINE get the FULL
            # stagger (they're not urgent — they were already stuck).
            import random as _r
            from streamonitor.enums import Status as _S
            _was_active = self.previous_status in (_S.PUBLIC, _S.RECORDING) \
                          if hasattr(self, "previous_status") and self.previous_status else False
            if not getattr(self, "_is_boot_bot", True):
                # Added via the UI after boot -> single bot, no thundering herd.
                # Record promptly instead of waiting up to 5 min.
                _stagger = 0.0
            else:
                _stagger = _r.uniform(0, 30) if _was_active else _r.uniform(0, 300)
            self.logger.verbose(f"Bot main loop starting (stagger {_stagger:.1f}s, "
                                  f"was_active={_was_active} boot={getattr(self,'_is_boot_bot',True)})")
            self._sleep(_stagger)

            # Initialize offline accumulator (instance attr so restart()
            # can reset it). Keeps backward-compat with anywhere that
            # used to read self._offline_time before run() started.
            if not hasattr(self, '_offline_time'):
                self._offline_time = 0

            while self.running and not self.quitting:
                try:
                    self.recording = False
                    # Bulk-update sites (CB/SC/CS) get their status from the
                    # LiveManager bulk poller (ONE API call per site, not per
                    # bot). They self-poll getStatus only when NOTRUNNING (no
                    # status yet) -- which gives a freshly UI-ADDED model its
                    # initial status promptly without waiting on the bulk poller.
                    # That self-poll is SUPPRESSED during startup restore
                    # (suppress_boot_poll) because firing it for all 1000+
                    # restored bulk bots at once (CB via the proxy, with retries)
                    # saturated the GIL and stalled _restore()/app.run() for
                    # minutes. Non-bulk sites have no bulk poller, so always poll.
                    if not self.bulk_update or (
                            self.sc == Status.NOTRUNNING
                            and not type(self).suppress_boot_poll):
                        try:
                            self.sc = self.getStatus()
                        except Exception as e:
                            self.logger.exception(e)
                            self.sc = Status.ERROR
                    # Check if the status has changed and log the update if it's different from the previous status
                    if self.sc != self.previous_status:
                        self.log(self.status())
                        self.previous_status = self.sc
                    # A model that's no longer PUBLIC (left / went private / offline)
                    # clears the consecutive-failure tally so it returns fresh and a
                    # later single blip doesn't immediately trip the ERROR escalation.
                    if self.sc != Status.PUBLIC:
                        self._consec_dl_fail = 0
                    # Feed the VPN auto-rotator: a rate-limited exit IP (HTTP
                    # 429 / Cloudflare 403 -> Status.RATELIMIT) is the signal to
                    # rotate the Mullvad location and retry on a fresh IP. No-op
                    # unless rotation is configured (vpn_config.json / env).
                    if self.sc == Status.RATELIMIT:
                        try:
                            from streamonitor.utils import vpn_rotator as _vpn
                            _vpn.report_ratelimit(self.siteslug or self.site or "?")
                        except Exception:
                            pass
                    if self.sc == Status.ERROR:
                        self._sleep(self.sleep_on_error)
                    if self.sc == Status.OFFLINE:
                        self._offline_time += self.sleep_on_offline
                        if self._offline_time > self.long_offline_timeout:
                            self.sc = Status.LONG_OFFLINE
                    elif self.sc == Status.PUBLIC or self.sc == Status.PRIVATE:
                        self._offline_time = 0
                        if self.sc == Status.PUBLIC:
                            if self.cookie_update_interval > 0 and self.cookieUpdater is not None:
                                def update_cookie():
                                    while self.sc == Status.PUBLIC and not self.quitting and self.running:
                                        self._sleep(self.cookie_update_interval)
                                        ret2 = self.cookieUpdater()
                                        if ret2:
                                            self.debug('Updated cookies')
                                        else:
                                            self.logger.warning('Failed to update cookies')
                                cookie_update_process = Thread(target=update_cookie)
                                cookie_update_process.start()

                            try:
                                video_url = self.getVideoUrl()
                            except Exception as e:
                                self.logger.debug(f'getVideoUrl failed (transient?): {e}')
                                video_url = None
                            if not video_url:
                                # No stream right now -- almost always the model
                                # just left public between the poll and this fetch,
                                # or a transient CDN/DNS blip that self-heals. Don't
                                # flap to ERROR / log ERROR for that; only escalate
                                # after several CONSECUTIVE failures. (`not video_url`
                                # also catches the [] some sites return.)
                                self._consec_dl_fail += 1
                                if self._consec_dl_fail >= 3:
                                    self.sc = Status.ERROR
                                    if self._consec_dl_fail == 3:  # log ONCE on escalation, not every cycle
                                        self.logger.warning(self.status())
                                else:
                                    self.logger.debug(
                                        f'No stream URL (likely left public); re-polling '
                                        f'[{self._consec_dl_fail}/3]')
                                # Back off a stuck-but-"public" model (e.g. ticket/private
                                # show the affiliate API still lists as online) so it
                                # doesn't retry and re-log every cycle.
                                self._sleep(self.sleep_on_error * min(self._consec_dl_fail, 4))
                                continue
                            self.log('Started downloading show')
                            self.recording = True
                            try:
                                file = self.genOutFilename()
                            except ModelFolderUnavailable as e:
                                # This model's folder is corrupted on disk.
                                # chkdsk territory, not something a retry fixes,
                                # so say it once and park the model on a long
                                # backoff instead of failing every poll.
                                self.recording = False
                                if not getattr(self, "_folder_corrupt_logged", False):
                                    self._folder_corrupt_logged = True
                                    self.logger.error(
                                        f"{e} — parking this model; run chkdsk "
                                        f"on the recordings drive to repair it")
                                self.sc = Status.ERROR
                                self._sleep(max(self.sleep_on_error, 300))
                                continue
                            except RecordingsDirUnavailable:
                                # Configured drive is detached. Hold this model
                                # (no spill to the system disk) and re-poll; the
                                # single explanatory line was already logged by
                                # _recordings_base_ok(), so stay quiet here to
                                # avoid the per-bot traceback storm this
                                # replaced. Not a download failure, so the
                                # consecutive-failure tally is left alone.
                                self.recording = False
                                self._sleep(self.sleep_on_error)
                                continue
                            try:
                                ret = _record_under_cap(self, video_url, file)
                            except Exception as e:
                                self.logger.exception(e)
                                ret = False
                            if not ret:
                                self.recording = False
                                # A recording that ended with no data is usually the
                                # model leaving / a transient CDN drop -- same
                                # consecutive-failure gate as the no-URL case so the
                                # card doesn't flap red on every blip.
                                self._consec_dl_fail += 1
                                if self._consec_dl_fail >= 3:
                                    self.sc = Status.ERROR
                                    if self._consec_dl_fail == 3:  # log ONCE on escalation
                                        self.log('Recording ended with error')
                                        self.log(self.status())
                                else:
                                    self.logger.debug(
                                        f'Recording ended early (transient); re-polling '
                                        f'[{self._consec_dl_fail}/3]')
                                self._sleep(self.sleep_on_error * min(self._consec_dl_fail, 4))
                                continue
                            self.recording = False
                            self._consec_dl_fail = 0
                            self.log('Recording ended')
                            # NO automatic pruning. Retention is handled by a
                            # separate script outside the recorder. A recorder
                            # that deletes its own output on every completed
                            # capture is one bug away from destroying the
                            # library it just built. _prune_old_recordings
                            # stays available for an explicit, deliberate call;
                            # nothing invokes it automatically.
                            try:
                                self.cache_file_list()
                            except Exception as e:
                                self.logger.exception(e)
                except Exception as e:
                    self.logger.exception(e)
                    try:
                        self.cache_file_list()
                    except Exception as e:
                        self.logger.exception(e)
                    self.log(self.status())
                    self.recording = False
                    self._sleep(self.sleep_on_error)
                    continue

                if self.quitting:
                    break
                elif self.bulk_update:
                    self._sleep(1)
                elif self.ratelimit:
                    self._sleep(self.sleep_on_ratelimit)
                elif self._offline_time > self.long_offline_timeout:
                    self._sleep(self.sleep_on_long_offline)
                elif self.sc == Status.PRIVATE:
                    self._sleep(self.sleep_on_private)
                else:
                    self._sleep(self.sleep_on_offline)

                # Adaptive backoff: when a bot has many consecutive errors
                # (Status.ERROR streak), exponentially extend its polling
                # interval. Without this, ~500 chronically-erroring bots
                # poll every 20s and exhaust Windows socket handles
                # (WinError 1450 — silent in old logs because
                # str(WindowsError) was empty). Cap at 5 minutes so a
                # recovering bot doesn't take an hour to come back.
                if self.sc == Status.ERROR and self._consecutive_errors > 0:
                    extra = min(300, int(2 ** min(self._consecutive_errors, 6)) * 5)
                    if extra > 0:
                        self._sleep(extra)

            self.sc = Status.NOTRUNNING
            self.log("Stopped")

        except KeyboardInterrupt:
            self.logger.info("Bot interrupted")
        except Exception as e:
            self.logger.exception(f"Fatal error in bot thread: {e}")
        finally:
            self._stop_cookie_updater()
            self.sc = Status.NOTRUNNING
            self.log(colored("Bot stopped", "red", attrs=["bold"]))

    def getPlaylistVariants(self, url: Optional[str] = None, m3u_data: Optional[Union[str, m3u8.M3U8]] = None) -> Optional[List[Dict[str, Any]]]:
        """Parse M3U8 playlist and extract available quality variants."""
        sources = []

        try:
            if isinstance(m3u_data, m3u8.M3U8):
                variant_m3u8 = m3u_data
            elif isinstance(m3u_data, str):
                variant_m3u8 = m3u8.loads(m3u_data)
            elif url:
                try:
                    result = self.session.get(
                        url,
                        headers=self.headers,
                        bucket='hls',
                        timeout=30
                    )
                    
                    if result.status_code != 200:
                        # Transient most of the time (model left -> 404, private ->
                        # 403). The run loop's consecutive-failure gate escalates a
                        # genuinely-stuck model (ERROR badge in the UI), so this
                        # per-blip line is debug-only to keep the log quiet.
                        self.logger.debug(f"Playlist fetch HTTP {result.status_code} (transient)")
                        return None
                    
                    m3u8_doc = result.text
                    
                    if not m3u8_doc.strip().startswith("#EXTM3U"):
                        self.logger.error(f"Invalid M3U8 data. Response: {result.text[:200]}")
                        return None
                    
                    variant_m3u8 = m3u8.loads(m3u8_doc)
                    
                except Exception as e:
                    self.logger.error(f"Error fetching playlist: {e}")
                    return None
            else:
                return sources

            for playlist in variant_m3u8.playlists:
                stream_info = playlist.stream_info
                resolution = stream_info.resolution if isinstance(stream_info.resolution, tuple) else (0, 0)
                sources.append({
                    'url': playlist.uri,
                    'resolution': resolution,
                    'frame_rate': stream_info.frame_rate,
                    'bandwidth': stream_info.bandwidth
                })

            if not variant_m3u8.is_variant and len(sources) >= 1:
                self.logger.warning("Not a variant playlist, can't select resolution")
                return None
            
            return sources
            
        except Exception as e:
            self.logger.error(f"Error parsing playlist variants: {e}")
            return None

    def getWantedResolutionPlaylist(self, url: str) -> Optional[str]:
        try:
            sources = self.getPlaylistVariants(url)
            if not sources:
                # Usually the model just left public / a transient CDN blip; the
                # run loop gates ERROR on consecutive failures, so this is debug.
                self.logger.debug("No available sources (model likely left public)")
                return None

            for source in sources:
                width, height = source['resolution']
                if width < height:
                    source['resolution_diff'] = width - WANTED_RESOLUTION
                else:
                    source['resolution_diff'] = height - WANTED_RESOLUTION

            sources.sort(key=lambda a: abs(a['resolution_diff']))
            selected_source = None

            if WANTED_RESOLUTION_PREFERENCE == 'exact':
                if sources[0]['resolution_diff'] == 0:
                    selected_source = sources[0]
            elif WANTED_RESOLUTION_PREFERENCE == 'closest' or len(sources) == 1:
                selected_source = sources[0]
            elif WANTED_RESOLUTION_PREFERENCE == 'exact_or_least_higher':
                for source in sources:
                    if source['resolution_diff'] >= 0:
                        selected_source = source
                        break
            elif WANTED_RESOLUTION_PREFERENCE == 'exact_or_highest_lower':
                for source in sources:
                    if source['resolution_diff'] <= 0:
                        selected_source = source
                        break
            else:
                self.logger.error('Invalid value for WANTED_RESOLUTION_PREFERENCE')
                return None

            if not selected_source:
                self.logger.error("Couldn't select a resolution")
                return None

            w, h = selected_source['resolution']
            if h != 0:
                frame_rate = ''
                if selected_source.get('frame_rate'):
                    frame_rate = f" {selected_source['frame_rate']}fps"
                self.logger.info(f"Selected {w}x{h}{frame_rate} resolution")

            selected_source_url = selected_source['url']
            if url:
                result_url = urljoin(url, selected_source_url)
                # Preserve query params from original URL if the variant URL doesn't have its own
                if '?' not in selected_source_url and '?' in url:
                    original_query = url.split('?', 1)[1]
                    result_url += ('&' if '?' in result_url else '?') + original_query
            else:
                result_url = selected_source_url
            return result_url

        except Exception as e:
            self.logger.error(f"Error selecting resolution: {e}")
            traceback.print_exc()
            return None

    def getVideoUrl(self) -> Optional[str]:
        pass

    def progressInfo(self, p: Dict[str, Any]) -> None:
        if p['status'] == 'downloading':
            try:
                pct = round(float(p['downloaded_bytes']) / float(p['total_bytes']) * 100, 1)
                self.log(colored(f"Downloading {pct}%", "blue"))
            except Exception:
                pass
        elif p['status'] == 'finished':
            self.log(colored(f"Recording ended. File: {p['filename']}", "green"))

    @property
    def outputFolder(self) -> str:
        # Plain path join, never raising: read-only callers (folder scans, the
        # dashboard's recorded-size column) must keep working while the drive
        # is away. The availability guard lives in genOutFilename(), the only
        # path that actually needs to CREATE anything.
        base_folder = os.path.join(DOWNLOADS_DIR, f"{self.username} [{self.siteslug}]")
        if hasattr(self, 'isMobile') and callable(getattr(self, 'isMobile', None)):
            try:
                if self.isMobile():
                    base_folder = os.path.join(base_folder, 'Mobile')
            except Exception:
                pass
        return base_folder

    def genOutFilename(self, create_dir: bool = True) -> str:
        """
        Thread-safe filename generation with file locking.
        Prevents race conditions when multiple bots run simultaneously.
        """
        with _filename_lock:
            folder = self.outputFolder
            if create_dir:
                # Check the drive first so a missing E:\ raises the dedicated
                # RecordingsDirUnavailable (logged once, held quietly) instead
                # of a per-bot FileNotFoundError traceback on every retry.
                _recordings_base()
                os.makedirs(folder, exist_ok=True)

            ext = f".{CONTAINER}".lower()
            
            # Create lock file for this folder
            lock_file = os.path.join(folder, ".filename.lock")
            lock = filelock.FileLock(lock_file, timeout=10)
            
            try:
                with lock:
                    # Clean up zero-byte files
                    try:
                        entries = [f for f in os.listdir(folder) if os.path.isfile(os.path.join(folder, f))]
                    except (FileNotFoundError, OSError) as e:
                        self.logger.warning(f"Error scanning video folder: {e}")
                        entries = []
                    
                    for f in entries:
                        if f.lower().endswith(ext):
                            p = os.path.join(folder, f)
                            try:
                                if os.path.getsize(p) == 0:
                                    if _may_delete(p, self.logger, "zero-byte sweep"):
                                        os.remove(p)
                                    self.logger.debug(f"Deleted zero-byte file: {f}")
                            except Exception as e:
                                self.logger.debug(f"Error checking/removing {f}: {e}")

                    # Refresh listing
                    try:
                        entries = [f for f in os.listdir(folder) if os.path.isfile(os.path.join(folder, f))]
                    except (FileNotFoundError, OSError):
                        entries = []

                    def sidecars_for(final_path: str) -> List[str]:
                        stem, _ = os.path.splitext(final_path)
                        return [
                            f"{stem}.tmp.ts",
                            f"{stem}.ts.tmp",
                            f"{stem}.segment.tmp",
                            f"{stem}.part",
                            f"{stem}.tmp",
                        ]

                    n: int = 1
                    while True:
                        candidate = os.path.join(folder, f"{n}{ext}")
                        
                        if os.path.exists(candidate):
                            try:
                                if os.path.getsize(candidate) > 0:
                                    n += 1
                                    continue
                                if _may_delete(candidate, self.logger, "zero-byte slot"):
                                    os.remove(candidate)
                                self.logger.debug(f"Removed zero-byte file during numbering: {n}{ext}")
                            except Exception:
                                n += 1
                                continue
                        
                        blocked = False
                        for sidecar in sidecars_for(candidate):
                            if os.path.exists(sidecar):
                                try:
                                    if os.path.getsize(sidecar) == 0:
                                        if _may_delete(sidecar, self.logger, "zero-byte sidecar"):
                                            os.remove(sidecar)
                                        self.logger.debug(f"Removed zero-byte sidecar: {os.path.basename(sidecar)}")
                                    else:
                                        blocked = True
                                        break
                                except Exception:
                                    blocked = True
                                    break
                        
                        if not blocked:
                            return candidate
                        
                        n += 1
            except filelock.Timeout:
                self.logger.warning("Filename lock timeout, proceeding without lock")
                return os.path.join(folder, f"1{ext}")
            except OSError as e:
                # Returning a filename here regardless meant the caller then
                # failed to OPEN it, treated that as a normal download failure,
                # and retried on the next poll — forever. One model with a
                # corrupted folder logged this 169 times in six hours and could
                # never record.
                #
                # exFAT (which the recordings drive uses) has no journal, so a
                # hard kill during writes can leave a directory entry that
                # lists fine but rejects every create with EINVAL / WinError
                # 1392 "corrupted and unreadable". That is not recoverable from
                # in-process — it needs chkdsk — so stop hammering it.
                if _folder_is_corrupt(folder):
                    raise ModelFolderUnavailable(
                        f"{folder}: unreadable/corrupted on disk ({e})") from e
                self.logger.error(f"OS error during filename generation: {e}")
                return os.path.join(folder, f"1{ext}")

    # Video extensions we own. Deliberately excludes .filename.lock and any
    # partial temp artefacts we don't recognise.
    _PRUNABLE_EXT = (".ts", ".mkv", ".mp4")

    def _prune_old_recordings(self, keep: int, protect: str = "",
                              confirm: bool = False) -> int:
        """Delete all but the `keep` newest recordings in this model's folder.

        This DELETES user data, so it is deliberately conservative:
          * no-op unless keep > 0 (the default is 0 = keep everything)
          * only touches known video extensions
          * never touches the file just written (`protect`) or anything
            modified in the last 60s, so an in-flight capture is safe
          * a failure to delete one file never aborts the rest
        """
        # DELETES RECORDINGS. Three hard gates, because losing captures is
        # worse than any amount of disk pressure:
        #
        #   1. confirm=True must be passed explicitly. Nothing in the recorder
        #      calls this automatically -- retention is a separate, external
        #      script that the user runs deliberately.
        #   2. Never while this model is recording. Deleting from a folder an
        #      ffmpeg process is writing into risks taking the in-flight file.
        #   3. keep must be > 0. keep=0 means "keep everything", never "delete
        #      everything" -- an off-by-one there would wipe the library.
        if not confirm:
            self.logger.debug("prune refused: confirm=False (must be explicit)")
            return 0
        if keep <= 0:
            return 0
        if getattr(self, "recording", False):
            self.logger.info("prune skipped: this model is recording")
            return 0
        folder = self.outputFolder
        try:
            names = os.listdir(folder)
        except OSError:
            return 0
        protect_abs = os.path.abspath(protect) if protect else ""
        now = _wall_clock()
        vids = []
        for n in names:
            if not n.lower().endswith(self._PRUNABLE_EXT):
                continue
            p = os.path.join(folder, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if protect_abs and os.path.abspath(p) == protect_abs:
                continue
            if now - st.st_mtime < 60:      # still being written / just closed
                continue
            vids.append((st.st_mtime, p, st.st_size))
        if len(vids) <= keep:
            return 0
        vids.sort(reverse=True)             # newest first
        removed = freed = 0
        for _, p, size in vids[keep:]:
            try:
                # Counters INSIDE the guard: they previously incremented even
                # when the delete was suppressed, so a fully-blocked prune
                # still logged "Pruned 4 old recording(s), freed N MB" -- a
                # log that claims data was destroyed when none was.
                if _may_delete(p, self.logger, "prune old recording"):
                    os.remove(p)
                    removed += 1
                    freed += size
            except OSError as e:
                self.logger.debug(f"prune: could not remove {p}: {e}")
        if removed:
            self.logger.info(
                f"Pruned {removed} old recording(s), freed "
                f"{freed / 1048576:.0f} MB (keeping newest {keep})")
        return removed

    def export(self) -> Dict[str, Any]:
        data = {
            "site": self.site,
            "username": self.username,
            "running": self.running,
            "status": self.sc.name if hasattr(self.sc, 'name') else str(self.sc),
            "recording": self.recording,
        }
        if self.gender != Gender.UNKNOWN:
            data["gender"] = self.gender.value
        if self.country:
            data["country"] = self.country
        return data

    @classmethod
    def fromConfig(cls, config: Dict[str, Any]) -> Optional['Bot']:
        """Create a Bot instance from a saved config dict (with gender/country restoration)."""
        username = config.get("username")
        if not username:
            return None
        instance = cls(username)
        gender_val = config.get("gender")
        if gender_val is not None:
            try:
                instance.gender = Gender(gender_val)
            except (ValueError, KeyError):
                instance.gender = Gender.UNKNOWN
        country = config.get("country")
        if country:
            instance.country = country
        return instance

    @staticmethod
    def str2site(site: str) -> Optional[Type['Bot']]:
        site = site.lower()
        for sitecls in Bot.loaded_sites:
            if site == sitecls.site.lower() or \
                    site == sitecls.siteslug.lower() or \
                    site in sitecls.aliases:
                return sitecls
        return None

    @staticmethod
    def createInstance(username: str, site: Optional[str] = None) -> Optional['Bot']:
        if site:
            site_cls = Bot.str2site(site)
            if site_cls:
                return site_cls(username)
        return None

    @staticmethod
    def register_manager(manager):
        """Register the manager instance for auto-removal functionality"""
        Bot._manager_instance = manager

    @staticmethod
    def auto_remove_model(username: str, site: str, reason: str = "deleted"):
        """Auto-remove a deleted/invalid model from the configuration"""
        if Bot._manager_instance is None:
            return False
        
        try:
            # Find the specific streamer to remove
            with Bot._manager_instance._streamers_lock:
                streamer_to_remove = None
                for streamer in Bot._manager_instance.streamers:
                    if (streamer.username.lower() == username.lower() and 
                        streamer.site.lower() == site.lower()):
                        streamer_to_remove = streamer
                        break
            
            if streamer_to_remove:
                # Use the existing removal logic from the manager
                result = Bot._manager_instance.do_remove(streamer_to_remove, username, site)
                Bot._manager_instance.logger.warning(f"🗑️ Auto-removed {reason} model [{site}] {username}")
                return True
            return False
            
        except Exception as e:
            if Bot._manager_instance:
                Bot._manager_instance.logger.error(f"Failed to auto-remove model {username}: {e}")
            return False


class RoomIdBot(Bot):
    """Base class for sites that use a numeric room_id (StripChat, Flirt4Free, SexChatHU, FanslyLive).
    
    Supports looking up username from room_id and vice versa.
    When instantiated with a numeric string, it's treated as a room_id.
    """
    site = None  # Must be set by subclass

    def __init__(self, username: str, room_id: Optional[str] = None) -> None:
        self.room_id: Optional[str] = room_id
        if room_id is None and username.isnumeric():
            self.room_id = username
        super().__init__(username)
        if self.room_id is None:
            self.room_id = self.getRoomIdFromUsername(username)
            if self.room_id:
                self.logger.debug(f'Found room ID: {self.room_id}')

    def getUsernameFromRoomId(self, room_id: str) -> Optional[str]:
        """Override in subclass to resolve username from room_id."""
        return None

    def getRoomIdFromUsername(self, username: str) -> Optional[str]:
        """Override in subclass to resolve room_id from username."""
        return None

    def export(self) -> Dict[str, Any]:
        data = super().export()
        if self.room_id:
            data["room_id"] = self.room_id
        return data

    @classmethod
    def fromConfig(cls, config: Dict[str, Any]) -> Optional['RoomIdBot']:
        username = config.get("username")
        if not username:
            return None
        room_id = config.get("room_id")
        instance = cls(username, room_id=room_id)
        gender_val = config.get("gender")
        if gender_val is not None:
            try:
                instance.gender = Gender(gender_val)
            except (ValueError, KeyError):
                instance.gender = Gender.UNKNOWN
        country = config.get("country")
        if country:
            instance.country = country
        return instance