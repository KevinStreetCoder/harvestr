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
        # Separate Live recordings from Archive downloads. Archive files
        # go to <output_dir>/<performer>/..., live recordings to
        # <live_output_dir>/<performer> [SITE]/N.mkv. By default
        # live_output_dir = <output_dir>/_live, but the user can override
        # it in the Live settings modal to put recordings on a different
        # drive (e.g. a secondary disk with more space for long streams).
        _LIVE_DIR = Path(__file__).resolve().parent / "downloads" / "_live"
        try:
            _cfg_path_early = Path(__file__).resolve().parent / "config.json"
            if _cfg_path_early.exists():
                _cfg_early = json.loads(_cfg_path_early.read_text(encoding="utf-8"))
                _user_live = (_cfg_early.get("live") or {}).get("live_output_dir") or ""
                if _user_live:
                    _LIVE_DIR = Path(_user_live).expanduser()
        except Exception:
            pass
        _LIVE_DIR.mkdir(parents=True, exist_ok=True)
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
        # On startup, reconstruct from config (do NOT auto-start — user clicks)
        self._restore()

    def _resolve_live_dir(self) -> Path:
        """Live recordings go to config.live.live_output_dir (if set) or
        downloads/_live/. Called at init time — same time the env var for
        StreaMonitor is set, so it's consistent with where recordings land."""
        try:
            cfg_path = Path(__file__).resolve().parent / "config.json"
            if cfg_path.exists():
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                live_dir = (cfg.get("live") or {}).get("live_output_dir") or ""
                if live_dir:
                    p = Path(live_dir).expanduser()
                    p.mkdir(parents=True, exist_ok=True)
                    return p
        except Exception as e:
            log.debug(f"[live] resolve live_dir: {e}")
        p = self.downloads_dir / "_live"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def model_folder(self, username: str, site: str) -> Path:
        """Where this model's recordings live on disk."""
        # StreaMonitor's output layout: <live_dir>/<username> [SITESLUG]/
        site_cls = SITES.get(site)
        slug = getattr(site_cls, "siteslug", site) if site_cls else site
        return self.live_dir / f"{username} [{slug}]"

    def repair_model(self, username: str, site: str, *,
                      delete_if_unfixable: bool = False) -> Dict[str, Any]:
        """Run the 3-tier repair pipeline over one model's recordings folder.
        Skips files that are currently locked by the recorder."""
        try:
            import video_repair
        except ImportError as e:
            return {"error": f"video_repair import failed: {e}"}
        folder = self.model_folder(username, site)
        if not folder.exists():
            return {"error": f"no folder at {folder}", "username": username, "site": site}
        log.info(f"[live] repair sweep: {folder}")
        results = video_repair.sweep_folder(
            str(folder), recursive=True,
            delete_if_unfixable=delete_if_unfixable,
            skip_if_locked=True, log=log,
        )
        summary = video_repair.summarize(results)
        summary["username"] = username
        summary["site"] = site
        summary["folder"] = str(folder)
        return summary

    def repair_all(self, *, delete_if_unfixable: bool = False,
                    only_recent_hours: float = 0.0) -> Dict[str, Any]:
        """Sweep every model's folder under the live directory. Optionally
        limit to files modified in the last N hours (for frequent
        scheduled runs that shouldn't re-touch old recordings)."""
        try:
            import video_repair
        except ImportError as e:
            return {"error": f"video_repair import failed: {e}"}
        log.info(f"[live] repair-all sweep: {self.live_dir} (recent_hours={only_recent_hours})")
        results = video_repair.sweep_folder(
            str(self.live_dir), recursive=True,
            delete_if_unfixable=delete_if_unfixable,
            only_recent_seconds=only_recent_hours * 3600 if only_recent_hours else 0,
            skip_if_locked=True, log=log,
        )
        return video_repair.summarize(results)

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
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            u = (entry.get("username") or "").strip()
            s = (entry.get("site") or "").strip()
            rid = entry.get("room_id")
            if not u or not s:
                continue
            try:
                self.add_model(u, s, room_id=rid)
                added += 1
            except Exception as e:
                errors.append(f"{u}|{s}: {e}")
        return {"ok": True, "added": added, "errors": errors,
                "total": len(self._models)}

    def add_model(self, username: str, site: str,
                  room_id: Optional[str] = None) -> Dict[str, Any]:
        username = (username or "").strip()
        site = (site or "").strip()
        if not username:
            raise ValueError("username required")
        self._create_bot(username, site, room_id=room_id, autostart=False)
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

        with self._lock:
            for _, rm in sorted(self._models.items(),
                                key=lambda kv: (kv[1].site, kv[1].username.lower())):
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
            },
            "models": models,
        }

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
